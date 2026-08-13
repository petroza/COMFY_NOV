# -*- coding: utf-8 -*-
"""Převod workflow z ComfyUI (UI export) do API formátu, který appka posílá.

ComfyUI umí uložit workflow dvěma způsoby:

* **UI export** — to, co vypadne z „Workflow → Export": pole `nodes`, `links`
  a někdy `definitions.subgraphs`. Je to podklad pro editor, ne pro API.
* **API export** — „Workflow → Export (API)": plochý slovník
  `{id: {class_type, inputs}}`. Tohle appka posílá na `/prompt`.

Appka umí jen ten druhý. Tenhle modul udělá první na druhý, včetně rozbalení
subgrafů — jinak by z workflow zůstala jen prázdná skořápka, protože celý
pipeline je schovaný v `definitions.subgraphs`.

Zásadní past: v UI exportu jsou hodnoty parametrů uložené jako **pole
`widgets_values` bez jmen**. Jména se musí vzít odjinud a **hádat se nesmí** —
špatně přiřazený parametr znamená spadlý nebo tichý špatný render. Zdroje jmen,
v tomhle pořadí:

1. `object_info` ze živého ComfyUI — autorita, zná přesné pořadí vstupů.
2. Vstupy, které si UI export sám pojmenoval (`inputs[].widget.name`).
3. Naše vlastní API šablony ve `workflows/` — ověřená pravda, s nimi se renderuje.

Když ani to nestačí, převod **skončí chybou a vypíše, co chybí**. Nikdy nevrátí
workflow, u kterého si není jistý.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("comfylocal.import")

# Typy, které v ComfyUI vždycky přicházejí drátem, ne jako hodnota v UI.
LINK_ONLY_TYPES: Set[str] = {
    "IMAGE", "MASK", "LATENT", "MODEL", "CLIP", "VAE", "CONDITIONING", "AUDIO",
    "VIDEO", "NOISE", "GUIDER", "SAMPLER", "SIGMAS", "CLIP_VISION", "CONTROL_NET",
    "STYLE_MODEL", "UPSCALE_MODEL", "LATENT_UPSCALE_MODEL", "IC_LORA_PARAMETERS",
    "PHOTOMAKER", "GLIGEN", "WEBCAM",
}

# Nody, které existují jen pro člověka v editoru — do API nepatří.
UI_ONLY_NODES: Set[str] = {"MarkdownNote", "Note", "PrimitiveNode"}

# Hodnoty, které UI přidává za číselný widget („co se seedem po generování").
# Nejsou to vstupy nodu, takže se musí přeskočit.
CONTROL_VALUES: Set[str] = {"fixed", "increment", "decrement", "randomize"}


class ImportError_(Exception):
    """Převod nelze udělat bezpečně."""


def is_link_only(type_name: Any) -> bool:
    """True, když se daný typ nedá zadat jako hodnota, jen připojit drátem."""
    parts = [p.strip().upper() for p in str(type_name or "").split(",") if p.strip()]
    return bool(parts) and all(p in LINK_ONLY_TYPES for p in parts)


# ── zdroje jmen parametrů ───────────────────────────────────
def names_from_object_info(object_info: Dict[str, Any], class_type: str) -> Optional[List[str]]:
    """Jména vstupů, které se v UI zobrazují jako widget, v pořadí definice."""
    spec = (object_info or {}).get(class_type)
    if not isinstance(spec, dict):
        return None
    inputs = spec.get("input")
    if not isinstance(inputs, dict):
        return None
    names: List[str] = []
    for section in ("required", "optional"):
        block = inputs.get(section)
        if not isinstance(block, dict):
            continue
        for name, cfg in block.items():
            type_name = cfg[0] if isinstance(cfg, (list, tuple)) and cfg else cfg
            # Seznam možností (combo) je taky widget, i když je to pole.
            if isinstance(type_name, list):
                names.append(name)
            elif not is_link_only(type_name):
                names.append(name)
    return names


def names_from_ui_node(node: Dict[str, Any]) -> List[str]:
    """Jména, která si UI export u widgetů poznamenal sám."""
    out: List[str] = []
    for item in node.get("inputs") or []:
        widget = item.get("widget") or {}
        name = widget.get("name")
        if name:
            out.append(str(name))
    return out


def known_names_from_templates(workflows_dir: Path) -> Dict[str, List[str]]:
    """Jména vstupů odečtená z našich API šablon.

    Tyhle šablony se reálně renderují, takže jsou jména ověřená. Slouží jako
    záloha, když appka nemá ComfyUI po ruce.
    """
    known: Dict[str, List[str]] = {}
    for path in sorted(Path(workflows_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for node in data.values():
            if not isinstance(node, dict):
                continue
            class_type = node.get("class_type")
            inputs = node.get("inputs")
            if not class_type or not isinstance(inputs, dict):
                continue
            for name in inputs:
                known.setdefault(str(class_type), [])
                if name not in known[str(class_type)]:
                    known[str(class_type)].append(str(name))
    return known


# ── rozbalení subgrafů ──────────────────────────────────────
def _link_index(links: Sequence[Any]) -> Dict[int, Tuple[Any, int]]:
    """{link_id: (origin_node_id, origin_slot)} — UI má dva různé tvary linků."""
    index: Dict[int, Tuple[Any, int]] = {}
    for link in links or []:
        if isinstance(link, (list, tuple)) and len(link) >= 5:
            index[int(link[0])] = (link[1], int(link[2]))
        elif isinstance(link, dict) and "id" in link:
            index[int(link["id"])] = (link.get("origin_id"), int(link.get("origin_slot") or 0))
    return index


class _Converter:
    def __init__(self, object_info: Optional[Dict[str, Any]],
                 known: Optional[Dict[str, List[str]]]) -> None:
        self.object_info = object_info or {}
        self.known = known or {}
        self.api: Dict[str, Dict[str, Any]] = {}
        self.problems: List[str] = []
        self.notes: List[str] = []
        self._counter = 0
        # {id instance subgrafu: {slot: zdroj}} — instance po rozbalení zmizí,
        # takže odkazy na ni se pak musí přesměrovat dovnitř.
        self._subgraph_outputs: Dict[str, Dict[int, Any]] = {}

    # -- jména widgetů pro konkrétní node --------------------
    def widget_names(self, node: Dict[str, Any]) -> Tuple[List[str], str]:
        class_type = str(node.get("type") or "")
        from_oi = names_from_object_info(self.object_info, class_type)
        if from_oi:
            return from_oi, "object_info"
        from_ui = names_from_ui_node(node)
        values = node.get("widgets_values")
        count = len(values) if isinstance(values, list) else 0
        if len(from_ui) >= count and from_ui:
            return from_ui, "UI export"
        if class_type in self.known:
            return list(self.known[class_type]), "naše šablony"
        if from_ui:
            return from_ui, "UI export (částečně)"
        return [], "nezjištěno"

    def map_widgets(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """Přiřadí `widgets_values` ke jménům vstupů.

        Pozice se počítají přes **všechna** jména widgetů, i ta, která jsou
        zvenku přebitá drátem — jejich hodnota totiž v `widgets_values` stejně
        zabírá místo. Kdyby se přeskočila, všechny další parametry by se
        posunuly o jeden a render by dostal nesmysly.
        """
        values = node.get("widgets_values")
        if not isinstance(values, list) or not values:
            return {}
        class_type = str(node.get("type") or "")
        names, source = self.widget_names(node)

        out: Dict[str, Any] = {}
        vi = 0
        for name in names:
            if vi >= len(values):
                break
            out[name] = values[vi]
            vi += 1
            # Za číslem bývá volba „co se seedem po generování" — není to vstup.
            if vi < len(values) and isinstance(values[vi], str) and values[vi] in CONTROL_VALUES:
                vi += 1

        # Zbyly nepřiřazené hodnoty → jménům nevěříme, radši to nahlásíme.
        leftovers = [v for v in values[vi:] if not (isinstance(v, str) and v in CONTROL_VALUES)]
        if leftovers:
            self.problems.append(
                f"{class_type}: neumím přiřadit {len(leftovers)} hodnot "
                f"(jména ze zdroje „{source}“: {names or '—'}; nepřiřazeno: {leftovers!r:.120})")
        return out

    # -- převod jednoho grafu --------------------------------
    def emit(self, prefix: str, node: Dict[str, Any], inputs: Dict[str, Any]) -> str:
        node_id = f"{prefix}{node.get('id')}"
        entry: Dict[str, Any] = {"class_type": str(node.get("type")), "inputs": inputs}
        title = node.get("title")
        if title:
            entry["_meta"] = {"title": str(title)}
        self.api[node_id] = entry
        return node_id

    def convert_graph(self, nodes: Sequence[Dict[str, Any]], links: Sequence[Any],
                      subgraphs: Dict[str, Dict[str, Any]], prefix: str,
                      external: Optional[Dict[int, Any]] = None) -> Dict[int, Any]:
        """Přeloží jeden graf. Vrací {výstupní slot: zdroj} pro subgrafy."""
        index = _link_index(links)
        external = external or {}

        def resolve(link_id: Any) -> Any:
            """Z link id udělá ["node", slot], nebo hodnotu u vstupu subgrafu."""
            if link_id is None:
                return None
            origin, slot = index.get(int(link_id), (None, 0))
            if origin is None:
                return None
            if int(origin) == -10:            # vstup subgrafu → co je zvenku
                return external.get(int(slot))
            return [f"{prefix}{origin}", int(slot)]

        outputs: Dict[int, Any] = {}
        for node in nodes:
            class_type = str(node.get("type") or "")
            if class_type in UI_ONLY_NODES:
                continue
            if node.get("mode") in (2, 4):     # vypnutý / obejitý node v editoru
                self.notes.append(f"{class_type} (#{node.get('id')}) je v editoru vypnutý — vynechán.")
                continue

            # Subgraf: rozbalíme ho na místě.
            if class_type in subgraphs:
                self.expand_subgraph(node, subgraphs[class_type], subgraphs, prefix, resolve)
                continue

            wired: Dict[str, Any] = {}
            for item in node.get("inputs") or []:
                name = (item.get("widget") or {}).get("name") or item.get("name")
                if not name or item.get("link") is None:
                    continue
                value = resolve(item.get("link"))
                if value is not None:
                    wired[str(name)] = value

            inputs = self.map_widgets(node)
            inputs.update(wired)      # drát má vždycky přednost před hodnotou
            self.emit(prefix, node, inputs)

        # Výstupy subgrafu: linky, které míří na node -20.
        for link in links or []:
            target = link[3] if isinstance(link, (list, tuple)) and len(link) >= 5 else (
                link.get("target_id") if isinstance(link, dict) else None)
            if target is None or int(target) != -20:
                continue
            slot = link[4] if isinstance(link, (list, tuple)) else link.get("target_slot")
            origin = link[1] if isinstance(link, (list, tuple)) else link.get("origin_id")
            o_slot = link[2] if isinstance(link, (list, tuple)) else link.get("origin_slot")
            outputs[int(slot or 0)] = [f"{prefix}{origin}", int(o_slot or 0)]
        return outputs

    def expand_subgraph(self, instance: Dict[str, Any], definition: Dict[str, Any],
                        subgraphs: Dict[str, Dict[str, Any]], prefix: str, resolve) -> None:
        """Vloží vnitřek subgrafu do výsledku a propojí ho s okolím."""
        self._counter += 1
        inner_prefix = f"{prefix}s{self._counter}:"

        # Co přiteče do každého vstupu subgrafu: buď drát zvenku, nebo hodnota
        # z widgetu na instanci. Hodnoty se berou v pořadí vstupů subgrafu,
        # ale link-only vstupy (obrázky) žádnou hodnotu neukusují.
        by_name = {}
        for item in instance.get("inputs") or []:
            name = item.get("name") or (item.get("widget") or {}).get("name")
            if name:
                by_name[str(name)] = item
        values = instance.get("widgets_values")
        values = list(values) if isinstance(values, list) else []

        external: Dict[int, Any] = {}
        vi = 0
        for slot, spec in enumerate(definition.get("inputs") or []):
            name = str(spec.get("name") or "")
            link_only = is_link_only(spec.get("type"))
            outer = by_name.get(name)
            wired = resolve(outer.get("link")) if outer and outer.get("link") is not None else None
            if wired is not None:
                external[slot] = wired
                if not link_only:
                    vi += 1          # widget existuje, jen je přebitý drátem
                continue
            if link_only:
                external[slot] = None
                continue
            if vi < len(values):
                external[slot] = values[vi]
                vi += 1
                if vi < len(values) and isinstance(values[vi], str) and values[vi] in CONTROL_VALUES:
                    vi += 1
            else:
                external[slot] = None
                self.problems.append(
                    f"subgraf {definition.get('name')!r}: vstup {name!r} nemá hodnotu ani drát.")

        inner_out = self.convert_graph(definition.get("nodes") or [], definition.get("links") or [],
                                       subgraphs, inner_prefix, external)
        # Výstup subgrafu se z pohledu okolí chová jako výstup instance.
        self._subgraph_outputs[str(instance.get("id"))] = inner_out


def convert_ui_workflow(ui: Dict[str, Any], object_info: Optional[Dict[str, Any]] = None,
                        known_names: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """UI export → API workflow. Při nejistotě vyhodí ImportError_."""
    if not isinstance(ui, dict) or "nodes" not in ui:
        raise ImportError_("Tohle není UI export z ComfyUI (chybí pole „nodes“).")

    subgraphs = {str(s.get("id")): s
                 for s in ((ui.get("definitions") or {}).get("subgraphs") or [])}
    conv = _Converter(object_info, known_names)
    conv.convert_graph(ui.get("nodes") or [], ui.get("links") or [], subgraphs, "")

    # Instance subgrafu zmizela, takže odkazy na ni se musí přesměrovat dovnitř.
    remap = {node_id: outs for node_id, outs in conv._subgraph_outputs.items()}
    if remap:
        for entry in conv.api.values():
            for key, value in list(entry["inputs"].items()):
                if isinstance(value, list) and len(value) == 2 and str(value[0]) in remap:
                    inner = remap[str(value[0])].get(int(value[1]))
                    if inner is None:
                        conv.problems.append(
                            f"výstup subgrafu {value[0]}:{value[1]} nemá kam vést.")
                    entry["inputs"][key] = inner

    dangling = [f"{nid}.{k}" for nid, e in conv.api.items()
                for k, v in e["inputs"].items() if v is None]
    if dangling:
        conv.problems.append("nepropojené vstupy: " + ", ".join(dangling[:10]))

    if conv.problems:
        raise ImportError_(
            "Převod nelze dokončit bezpečně, protože si nejsem jistý parametry:\n  - "
            + "\n  - ".join(conv.problems[:12])
            + "\n\nSpolehlivé řešení: v ComfyUI otevři workflow a dej "
              "„Workflow → Export (API)“ — takový soubor appka umí použít přímo. "
              "Nebo spusť import znovu, když appka vidí ComfyUI (pak si jména vstupů "
              "vezme z object_info a hádat nemusí).")
    if not conv.api:
        raise ImportError_("Po převodu nezůstal žádný node.")
    return conv.api


def model_files_in(workflow: Dict[str, Any]) -> List[str]:
    """Soubory modelů, na které workflow odkazuje (pro kontrolu dostupnosti)."""
    found: List[str] = []
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in (node.get("inputs") or {}).values():
            if isinstance(value, str) and value.lower().endswith(
                    (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".sft")):
                if value not in found:
                    found.append(value)
    return sorted(found)


__all__ = ["convert_ui_workflow", "known_names_from_templates", "model_files_in",
           "ImportError_", "is_link_only", "names_from_object_info"]
