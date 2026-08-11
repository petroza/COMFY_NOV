const API='api.php';const CSRF='';const JUST_LOGGED_IN=false;const DEFAULT_PROMPT_LANG='cs';const EXPECTED_WORKER_VERSION=(typeof APP_VERSION!=='undefined'?APP_VERSION:'1.0.0');let jobs=[],selectedId=null,detailCache=null;let selectedJobs=new Set(),lastSelectedId=null;let promptLang=DEFAULT_PROMPT_LANG;let currentPreviewUrl=null;let currentPreviewUrls=[];let selectedImageFiles=[];let authExpired=false;let restoreJobDone=false;
let selectedWorker='any';let workersState={};let selectedImageFile2=null;let currentPreviewUrl2=null;let imageCrop1=null,imageCrop2=null,cropDragState=null;
function randomSeed(){return Math.floor(Math.random()*2147483647)+1}
function freshSeed(){const el=$('seed');if(el)el.value=randomSeed()}
function updateMobSummary(){const s=$('mobSettingsSummary');if(!s)return;const preset=($('preset')?.value||'').substring(0,14);const fmt=$('format')?.value||'';const fl={'auto_fhd':'auto/FHD','fhd_landscape':'1920x1080','fhd_portrait':'1080x1920','classic_4_3':'1440x1080','classic_3_4':'1080x1440','hd_landscape':'1280x720','hd_portrait':'720x1280','square':'1024x1024','square_2000':'2000x2000','custom':'custom'};const seed=$('seed')?.value||'rnd';const sm=$('seedMode')?.value||'increment_batch';const smt=sm==='locked'?ui('locked','locked'):sm==='random_each'?ui('random','random'):ui('batch+','batch+');s.textContent=preset+' · '+(fl[fmt]||fmt)+' · seed '+seed+' · '+smt;}
function toggleMobSettings(){const btn=$('mobSettingsToggle'),sec=$('mobSettingsSection');if(!btn||!sec)return;const open=sec.classList.toggle('open');btn.classList.toggle('open',open);if(!open)updateMobSummary();}
function pickWorker(wid){selectedWorker=wid;document.querySelectorAll('.worker-btn').forEach(b=>b.classList.toggle('w-active',b.dataset.wid===wid));}
let comfyTargetInfo='',comfyTargetDot='';
function updateWorkerDots(workers){
  comfyTargetInfo='';comfyTargetDot='';
  workersState={};const now=Date.now();const picker=$('workerPicker');if(!picker)return;
  const groups={};
  for(const[wid,wx] of Object.entries(workers||{})){
    const prefix=wid.startsWith('DOMA-')?'DOMA':wid.startsWith('PRACE-')?'PRACE':wid;
    if(!groups[prefix])groups[prefix]={items:[],emoji:'💻',label:wid};
    if(prefix==='DOMA'){groups[prefix].emoji='🏠';groups[prefix].label='DOMA';}
    if(prefix==='PRACE'){groups[prefix].emoji='💼';groups[prefix].label=ui('PRÁCE','WORK');}
    const ts=wx.updated_at?new Date(wx.updated_at).getTime():0;
    const age=Math.abs(now-ts);
    const w=wx.worker||{};
    const activeJob=Number(w.active_job||0);
    const online=ts>0&&age<240000;
    // Delší render nesmí po pár minutách vypadat jako tvrdý offline.
    // Když worker naposledy hlásil aktivní job, držíme ho do 20 min jako „čekám na signál“.
    const signalWait=!online&&ts>0&&activeJob>0&&age<1200000;
    const stale=!online&&ts>0&&age<1200000;
    groups[prefix].items.push({wid,wx,online,stale,signalWait,activeJob,age});
  }
  Array.from(picker.querySelectorAll('.w-dynamic')).forEach(el=>el.remove());
  for(const[gkey,g] of Object.entries(groups)){
    const online=g.items.some(x=>x.online);const signalWait=!online&&g.items.some(x=>x.signalWait);const stale=!online&&g.items.some(x=>x.stale);
    const dotCls=online?'w-online':(signalWait||stale)?'w-stale':'w-offline';
    const x=g.items.find(x=>x.online)||g.items.find(x=>x.signalWait)||g.items[0];const gpu=x&&x.wx&&x.wx.gpu;const comfy=x&&x.wx&&x.wx.comfy||{};
    let info='offline';
    const comfyReady=online&&!!comfy.online;
    const comfyTxt=comfyReady?' · Comfy ready':(online&&comfy.state==='starting'?ui(' · Comfy startuje',' · Comfy starting'):'');
    if(online&&gpu)info=(gpu.name||'GPU')+((gpu.util_pct===null||gpu.util_pct===undefined)?'':' · GPU '+gpu.util_pct+'%')+' · VRAM '+(((gpu.mem_used_mb||0)/1024).toFixed(1))+'/'+(((gpu.mem_total_mb||0)/1024).toFixed(1))+' GB'+comfyTxt;
    else if(online)info='online'+comfyTxt;
    else if(signalWait)info=ui('render / čekám na signál','render / waiting for signal')+(x.activeJob?' · job #'+x.activeJob:'');
    else if(stale)info=ui('čekám na signál','waiting for signal');
    workersState[gkey]={online,stale,signalWait,items:g.items};
    // ComfyLocal má jediný cíl renderu (ComfyUI na síti), takže se nepřidává
    // tlačítko pro každý stroj — stav se píše do jediné karty níž.
    comfyTargetInfo=info;comfyTargetDot=dotCls;
  }
  const anyUsable=Object.values(workersState).some(x=>x.online||x.signalWait||x.stale);
  const anyOnline=Object.values(workersState).some(x=>x.online);
  const dotAny=$('wdot-any');if(dotAny)dotAny.className='wdot '+(anyOnline?'w-online':comfyTargetDot||'w-offline');
  const infoAny=$('winfo-any');
  if(infoAny)infoAny.textContent=comfyTargetInfo||(anyOnline?'online':ui('ComfyUI neodpovídá','ComfyUI not responding'));
  pickWorker('any');
}

let projectsList=[];let selectedProject=null;
async function loadProjects(){
  try{
    const d=await api('projects');
    if(!d.success)return;
    projectsList=d.projects||[];
    renderProjectCards();
  }catch(e){}
}
function projectIcon(t,p=null){return isTwoPictProject(p)?'🎞️':(t==='text'?'📝':t==='none'?'⚡':'🖼');}
function projectDisplayName(p){const name=String(p&&p.name||'');if(name.includes('nový model i2v'))return ui('LTX 2.3 nový model i2v / 1 PICT','LTX 2.3 new i2v model / 1 PICT');if(name.includes('první + poslední'))return ui('LTX 2.3 první + poslední frejm / 2 PICT','LTX 2.3 first + last frame / 2 PICT');return name||ui('Projekt ','Project ')+(p&&p.id?p.id:'');}
function renderProjectCards(){
  const sel=document.getElementById('project');
  if(!sel)return;
  if(!selectedProject&&projectsList.length){const first=projectsList.find(x=>!isTwoPictProject(x)&&String(x.input_type||'').toLowerCase()!=='none')||projectsList[0];selectedProject=first?+first.id:null;}
  const current=String(selectedProject||'');
  const opts=['<option value="">'+ui('Výchozí LTX image-to-video','Default LTX image-to-video')+'</option>'];
  projectsList.forEach(p=>{
    const icon=isTwoPictProject(p)?'🎞️ ':'🖼️ ';
    opts.push(`<option value="${esc(p.id)}">${icon}${esc(projectDisplayName(p))}</option>`);
  });
  sel.innerHTML=opts.join('');
  if(current&&[...sel.options].some(o=>o.value===current))sel.value=current;else sel.value='';
  selectProjectFromDropdown(true);
}
function selectProjectFromDropdown(silent=false){
  const sel=document.getElementById('project');
  selectedProject=sel&&sel.value?+sel.value:null;
  const hid=document.getElementById('selectedProjectId');if(hid)hid.value=selectedProject||'';
  const p=selectedProject?projectsList.find(x=>+x.id===+selectedProject):null;
  adaptFormToProject(p);
  updateModeUiFTP();
}
function selectProject(pid,el){
  const sel=document.getElementById('project');
  selectedProject=pid?+pid:null;
  if(sel)sel.value=selectedProject||'';
  const hid=document.getElementById('selectedProjectId');if(hid)hid.value=selectedProject||'';
  const p=selectedProject?projectsList.find(x=>+x.id===+selectedProject):null;
  adaptFormToProject(p);
  updateModeUiFTP();
}
function setPictModeFTP(mode){
  if(mode==='2'){
    const p=projectsList.find(x=>isTwoPictProject(x));
    if(!p){alert('2 PICT projekt ještě není v databázi. Spusť update/import projektů.');return;}
    selectProject(p.id,null);
  }else{
    const p=projectsList.find(x=>!isTwoPictProject(x)&&String(x.input_type||'').toLowerCase()!=='none');
    selectProject(p?p.id:null,null);
  }
}
function updateModeUiFTP(){
  const two=isCurrentTwoPict();
  const b1=document.getElementById('mode1Btn'),b2=document.getElementById('mode2Btn'),grid=document.getElementById('imageGrid'),title=document.getElementById('formTitle');
  if(b1)b1.classList.toggle('active',!two);if(b2)b2.classList.toggle('active',two);if(grid)grid.classList.toggle('two',two);
  if(title)title.textContent=two?ui('Nový LTX first/last-frame job','New LTX first/last-frame job'):ui('Nový LTX image-to-video job','New LTX image-to-video job');
}
function getSelectedProject(){return selectedProject?projectsList.find(x=>+x.id===+selectedProject):null}
function isTwoPictProject(p=getSelectedProject()){
  if(!p)return false;
  const hay=((p.name||'')+' '+(p.description||'')+' '+(p.workflow_file||'')+' '+(p.input_type||'')).toLowerCase();
  return hay.includes('flf2v')||hay.includes('2 pict')||hay.includes('2pict')||hay.includes('poslední')||hay.includes('posledni');
}
function isCurrentTwoPict(){return isTwoPictProject(getSelectedProject())}
function isPhotoEditProject(p=getSelectedProject()){
  if(!p)return false;
  const hay=((p.name||'')+' '+(p.description||'')+' '+(p.workflow_file||'')).toLowerCase();
  return hay.includes('flux2')||hay.includes('firered')||hay.includes('photo edit')||hay.includes('photo_edit')||hay.includes('úprava fotky')||hay.includes('uprava fotky');
}
function isCurrentPhotoEdit(){return isPhotoEditProject(getSelectedProject())}
function isImageOutput(j){return /\.(png|jpe?g|webp)(\?|$)/i.test(String((j&&(j.output_video||j.output_url))||''))}
function adaptFormToProject(p){
  const itype=p?p.input_type:'image';
  const sec=document.getElementById('imageSection');
  const sec2=document.getElementById('image2Section');
  const grid=document.getElementById('imageGrid');
  const two=isTwoPictProject(p);
  if(sec)sec.style.display=(itype==='image'||two)?'':'none';
  if(sec2){sec2.style.display=two?'':'none';sec2.classList.toggle('hidden',!two)}
  if(grid)grid.classList.toggle('two',two);
  const input=document.getElementById('imageInput');
  if(input)input.multiple=!two;
  const lab=document.getElementById('image1Label');
  if(lab)lab.textContent=two?ui('První frejm','First frame'):ui('Vstupní obrázek','Input image');
  const sel=document.getElementById('project');if(sel)sel.value=selectedProject||'';
  if(typeof updateModeUiFTP==='function')updateModeUiFTP();
  if(two&&selectedImageFiles.length>1){selectedImageFiles=selectedImageFiles.slice(0,1);try{const dt=new DataTransfer();dt.items.add(selectedImageFiles[0]);input.files=dt.files}catch(e){}renderImagePreview(selectedImageFiles)}
  // PHOTO EDIT (Flux.2 / FireRed): výstup je obrázek — schovej video sekce a přejmenuj tlačítko.
  const pe=isPhotoEditProject(p);
  document.querySelectorAll('section.acc[data-key="motion"]').forEach(el=>{el.style.display=pe?'none':''});
  const peBox=$('promptEnhanceBox');if(peBox)peBox.style.display=pe?'none':'';
  const tokEl=$('enhanceTokens');const tokField=tokEl?(tokEl.closest('.range-field')||tokEl.closest('.field')):null;if(tokField)tokField.style.display=pe?'none':'';
  // Kroky výpočtu / cfg / síla pohybu řídí jen photo edit. LTX 2.3 má pevný rozpis
  // sigem (ManualSigmas) a cfg = 1, takže by ty posuvníky jen mátly — u videa je schovej.
  const sliders=$('samplingSliders');if(sliders)sliders.style.display=pe?'':'none';
  const subBtn=document.querySelector('button[type="submit"].btn.primary');
  if(subBtn)subBtn.textContent=pe?ui('GENEROVAT OBRÁZEK','GENERATE IMAGE'):ui('GENEROVAT VIDEO','GENERATE VIDEO');
  const nvBtn=$('newVideoBtn');
  if(nvBtn)nvBtn.textContent=pe?ui('+ Nový obrázek','+ New image'):ui('+ Nové video','+ New video');
  const stepsHintEl=$('steps');if(pe&&stepsHintEl&&+stepsHintEl.value>30)stepsHintEl.value=20;
  // PHOTO EDIT: video negative ("shaky camera, jitter…") u fotky nedává smysl.
  const negEl=$('negativeInput');
  if(negEl){
    if(window._pzVideoNegDefault===undefined)window._pzVideoNegDefault=(negEl.defaultValue||negEl.value||'').trim();
    const cur=(negEl.value||'').trim();
    if(pe&&cur===window._pzVideoNegDefault)negEl.value='';
    if(!pe&&cur==='')negEl.value=window._pzVideoNegDefault;
  }
}

const $=id=>document.getElementById(id);const esc=s=>(s??'').toString().replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

let appLang='cs';
function ui(cs,en){return appLang==='en'?en:cs}
function setNodeText(id,cs,en){const el=$(id);if(el)el.textContent=ui(cs,en)}
function setNodeHtml(id,cs,en){const el=$(id);if(el)el.innerHTML=ui(cs,en)}
function setLabelFor(id,cs,en){const el=document.querySelector('label[for="'+id+'"]');if(el)el.textContent=ui(cs,en)}
function setSelOption(selectId,value,cs,en){const s=$(selectId);if(!s)return;const o=[...s.options].find(x=>x.value===value||x.textContent===cs||x.textContent===en);if(o)o.textContent=ui(cs,en)}
function setOptionTextByOriginal(selectId,cs,en){const s=$(selectId);if(!s)return;const opts=[...s.options];let o=opts.find(x=>x.dataset&&x.dataset.pzCs===cs)||opts.find(x=>x.value===cs||x.textContent.trim()===cs||x.textContent.trim()===en);if(!o)return;if(!o.dataset.pzCs){o.dataset.pzCs=cs;o.value=cs;}o.textContent=ui(cs,en)}
function translateOptionLabels(){
  const presetPairs=[['Decentní nájezd dopředu','Subtle push in'],['Pomalý nájezd dopředu','Slow push in'],['Pomalý odjezd dozadu','Slow pull back'],['Obíhání kolem objektu','Orbit around subject'],['Půlkruhový oblouk','Half-circle arc'],['Stoupání kamery (dron nahoru)','Camera rising / drone up'],['Klesání kamery (pohled dolů)','Camera descending / top view'],['Jeřáb nahoru','Crane up'],['Jeřáb dolů','Crane down'],['Pomalý posun do strany','Slow side move'],['Statická kamera (stativ)','Static camera (tripod)'],['Jemný posun (drobný drift)','Gentle drift'],['Z ruky (dokumentární)','Handheld documentary'],['Vlastní','Custom']];
  presetPairs.forEach(x=>setOptionTextByOriginal('preset',x[0],x[1]));
  setSelOption('project','', 'Výchozí LTX image-to-video','Default LTX image-to-video');
  setSelOption('format','auto_fhd','Auto podle fotky · FHD limit','Auto from image · FHD limit');setSelOption('format','fhd_landscape','FHD horizontal · 1920×1088','FHD landscape · 1920×1088');setSelOption('format','fhd_portrait','FHD vertical · 1088×1920','FHD portrait · 1088×1920');setSelOption('format','classic_4_3','4:3 · 1472×1088','4:3 · 1472×1088');setSelOption('format','classic_3_4','3:4 · 1088×1472','3:4 · 1088×1472');setSelOption('format','hd_landscape','HD horizontal · 1280×720','HD landscape · 1280×720');setSelOption('format','hd_portrait','HD vertical · 720×1280','HD portrait · 720×1280');setSelOption('format','square','Square · 1024×1024','Square · 1024×1024');setSelOption('format','square_2000','Square XL · 1984×1984','Square XL · 1984×1984');setSelOption('format','custom','Custom','Custom');
  setSelOption('seedMode','increment_batch','Batch: navyšovat seed','Batch: increment seed');setSelOption('seedMode','locked','Zamknout stejný seed','Lock same seed');setSelOption('seedMode','random_each','Náhodný pro každý obrázek','Random for each image');
}
function translateTextNodesExact(){
  const map={'Render jde přímo do ComfyUI, bez workeru a bez FTP.':'Rendering goes straight to ComfyUI — no worker, no FTP.','Cíl renderu':'Render target','Vybrat nebo přetáhnout obrázek':'Select or drag an image','Vybrat poslední frejm':'Select last frame','Defaultně vypnuto. U image-to-video může změnit prompt a oslabit držení vstupní fotky.':'Off by default. In image-to-video it can change the prompt and weaken input-image hold.','Délka vylepšeného promptu':'Enhanced prompt length','kratší':'shorter','delší / víc fantazie':'longer / more imagination','Platí jen při zapnutém Prompt Enhance. Doporučené: 128.':'Works only when Prompt Enhance is enabled. Recommended: 128.','Vyber job vlevo nebo vytvoř nový.':'Select a job on the left or create a new one.','Vyplň formulář a odešli video job.':'Fill the form and submit a video job.','Načítám frontu…':'Loading queue…','Označeno: 0':'Selected: 0'};
  const rev=Object.fromEntries(Object.entries(map).map(([k,v])=>[v,k]));
  const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode(n){const p=n.parentElement;if(!p)return NodeFilter.FILTER_REJECT;if(['SCRIPT','STYLE','TEXTAREA','INPUT','OPTION'].includes(p.tagName))return NodeFilter.FILTER_REJECT;const t=n.nodeValue.trim();return (map[t]||rev[t])?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;}});
  const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  nodes.forEach(n=>{const t=n.nodeValue.trim();const pre=n.nodeValue.match(/^\s*/)[0],post=n.nodeValue.match(/\s*$/)[0];if(appLang==='en'&&map[t])n.nodeValue=pre+map[t]+post;if(appLang==='cs'&&rev[t])n.nodeValue=pre+rev[t]+post;});
  document.querySelectorAll('#imgPreview.empty-preview,#imgPreview2.empty-preview').forEach(box=>{if(box.id==='imgPreview')box.innerHTML=imageEmptyPlaceholder();else box.innerHTML=image2EmptyPlaceholder();});
}
function translateStaticText(){
  (function(){var pe=(typeof isCurrentPhotoEdit==='function'&&isCurrentPhotoEdit());setNodeText('newVideoBtn',pe?'+ Nový obrázek':'+ Nové video',pe?'+ New image':'+ New video');})();setNodeText('diagBtn','Diagnostika','Diagnostics');setNodeText('selectAllBtn','Vybrat vše','Select all');setNodeText('selectFinishedBtn','Označit hotové','Select finished');setNodeText('clearSelectionBtn','Zrušit výběr','Clear selection');setNodeText('downloadSelectedBtn','Stáhnout označené','Download selected');setNodeText('downloadFinishedBtn','Stáhnout hotové','Download finished');setNodeText('deleteSelectedBtn','Smazat označené','Delete selected');setNodeText('clearFinishedBtn','Smazat hotové','Delete finished');
  setNodeText('formTitle',isCurrentTwoPict()?ui('Nový LTX first/last-frame job','New LTX first/last-frame job'):ui('Nový LTX image‑to‑video job','New LTX image-to-video job'),isCurrentTwoPict()?'New LTX first/last-frame job':'New LTX image-to-video job');
  const dt=$('detailTitle');if(dt&&(/^Detail jobu$|^Job detail$/.test(dt.textContent)))dt.textContent=ui('Detail jobu','Job detail');
  const theme=$('themeToggle');if(theme){const m=theme.querySelector('.moon'),s=theme.querySelector('.sun');if(m)m.textContent=ui('Světlý režim','Light mode');if(s)s.textContent=ui('Tmavý režim','Dark mode')}
  setNodeText('appViewBtn','App','App');setNodeText('setupViewBtn','Setup','Setup');setNodeText('stopBtn','STOP','STOP');setNodeText('downloadBtn','Stáhnout','Download');setNodeText('cancelBtn','Zrušit','Cancel');setNodeText('deleteBtn','Smazat','Delete');const uc=$('userChip');if(uc&&uc.querySelector('b'))uc.innerHTML=ui('uživatel','user')+' <b>'+esc(uc.querySelector('b').textContent)+'</b>';
  setNodeText('promptLangLabel','Jazyk promptu','Prompt language');setNodeText('langHint','CZ → EN automaticky na pozadí','EN only, no translation');
  const prompt=$('promptInput');if(prompt)prompt.placeholder=ui('česky: filmový realistický záběr, pomalý pohyb kamery...','English: cinematic realistic shot, slow camera movement...');
  const submit=document.querySelector('#jobForm button[type="submit"]');if(submit&&!submit.disabled)submit.textContent=ui('GENEROVAT VIDEO','GENERATE VIDEO');
  document.querySelectorAll('.acc-head span').forEach(sp=>{const v=sp.textContent.trim();const map={'01 Základ':'01 Basic','02 Prompt':'02 Prompt','03 Kamera a styl':'03 Camera & style','04 Video parametry':'04 Video parameters','05 Pokročilé řízení':'05 Advanced control','06 Odeslání':'06 Submit','01 Basic':'01 Basic','03 Camera & style':'03 Camera & style','04 Video parameters':'04 Video parameters','05 Advanced control':'05 Advanced control','06 Submit':'06 Submit'};const back={'01 Basic':'01 Základ','02 Prompt':'02 Prompt','03 Camera & style':'03 Kamera a styl','04 Video parameters':'04 Video parametry','05 Advanced control':'05 Pokročilé řízení','06 Submit':'06 Odeslání'};if(appLang==='en'&&map[v])sp.textContent=map[v];if(appLang==='cs'&&back[v])sp.textContent=back[v];});
  document.querySelectorAll('label').forEach(l=>{let v=l.textContent.trim();const en={'Projekt / workflow':'Project / workflow','První frejm / první obrázek':'First frame / first image','Vstupní obrázek':'Input image','Poslední frejm':'Last frame','Prompt':'Prompt','Negative prompt':'Negative prompt','Pohyb kamery — preset':'Camera motion — preset','Style preset':'Style preset','Rozlišení preset':'Resolution preset','Camera motion':'Camera motion','Style':'Style','Šířka':'Width','Výška':'Height','FPS':'FPS','Délka s':'Duration s','Seed':'Seed','Režim seedu':'Seed mode','Odeslat na počítač':'Send to computer','Délka vylepšeného promptu':'Enhanced prompt length'};const cs=Object.fromEntries(Object.entries(en).map(([k,v])=>[v,k]));if(appLang==='en'&&en[v])l.textContent=en[v];if(appLang==='cs'&&cs[v])l.textContent=cs[v];});
  document.querySelectorAll('.range-label span:first-child').forEach(sp=>{const v=sp.textContent.trim();const en={'Kroky výpočtu':'Steps','Držení promptu':'Prompt adherence','Síla pohybu':'Motion strength'};const cs={'Steps':'Kroky výpočtu','Prompt adherence':'Držení promptu','Motion strength':'Síla pohybu'};if(appLang==='en'&&en[v])sp.textContent=en[v];if(appLang==='cs'&&cs[v])sp.textContent=cs[v];});
  document.querySelectorAll('.range-scale span,.range-help,.small,.hint,.enhance-note').forEach(el=>{let v=el.textContent.trim();const en={'rychlejší':'faster','čistší':'cleaner','volnější':'looser','přesnější':'more precise','stabilní':'stable','živé':'more motion','kratší':'shorter','delší / víc fantazie':'longer / more imagination','Více kroků = čistší výsledek, ale delší generování.':'More steps = cleaner result, but slower generation.','Jak silně se model drží textu.':'How strongly the model follows the prompt.','Nižší hodnota stabilnější, vyšší víc pohybu.':'Lower is more stable, higher adds motion.','Render jde přímo do ComfyUI, bez workeru a bez FTP.':'PHP queue and worker stay unchanged. Run the downloaded worker on the ComfyUI PC.','Defaultně vypnuto. U image-to-video může změnit prompt a oslabit držení vstupní fotky.':'Off by default. In image-to-video it can change the prompt and weaken input-image hold.','Platí jen při zapnutém Prompt Enhance. Doporučené: 128.':'Works only when Prompt Enhance is enabled. Recommended: 128.','🇨🇿 prompt se přeloží na pozadí do angličtiny. 🇺🇸 odešle prompt bez překladu.':'In English mode the prompt is sent directly. In Czech mode it is translated to English in the background.'};const cs=Object.fromEntries(Object.entries(en).map(([k,v])=>[v,k]));if(appLang==='en'&&en[v])el.textContent=en[v];if(appLang==='cs'&&cs[v])el.textContent=cs[v];});
  const workerCard=document.querySelector('.worker-download-card b');if(workerCard)workerCard.textContent=ui('🖧 ComfyUI na síti','🖧 ComfyUI on the network');const wlink=document.querySelector('.worker-download-card .btn');if(wlink)wlink.textContent=ui('Otevřít ComfyUI','Open ComfyUI');
  const any=document.querySelector('#wbtn-any .wlabel');if(any)any.textContent=ui('🖧 ComfyUI','🖧 ComfyUI');const anyInfo=$('winfo-any');if(anyInfo&&/zjišťuji|checking/.test(anyInfo.textContent))anyInfo.textContent=ui('zjišťuji stav…','checking status…');
  translateOptionLabels();translateTextNodesExact();
}
function setAppLang(lang){appLang=(lang==='en')?'en':'cs';try{localStorage.setItem('pz_comfy_app_lang',appLang)}catch(e){}document.documentElement.lang=appLang;document.body.classList.toggle('app-en',appLang==='en');document.body.classList.toggle('app-cs',appLang==='cs');if($('appLangCsBtn'))$('appLangCsBtn').classList.toggle('active',appLang==='cs');if($('appLangEnBtn'))$('appLangEnBtn').classList.toggle('active',appLang==='en');setPromptLang(appLang==='cs'?'cs':'en');translateStaticText();if(projectsList&&projectsList.length)renderProjectCards();updateSelectionInfo();renderJobs();if(detailCache&&detailCache.job)renderDetail(detailCache.job,detailCache.events||[]);}
function getInitialAppLang(){try{return localStorage.getItem('pz_comfy_app_lang')||'cs'}catch(e){return 'cs'}}


function applyTheme(mode){const light=mode==='light';document.body.classList.toggle('light',light);try{localStorage.setItem('pz_comfy_theme',light?'light':'dark')}catch(e){}const btn=document.getElementById('themeToggle');if(btn){btn.classList.toggle('active',light);const moon=btn.querySelector('.moon'),sun=btn.querySelector('.sun');if(moon)moon.style.display=light?'none':'';if(sun)sun.style.display=light?'':'none';}}
function toggleTheme(){applyTheme(document.body.classList.contains('light')?'dark':'light')}
applyTheme((()=>{try{return localStorage.getItem('pz_comfy_theme')||'dark'}catch(e){return 'dark'}})());
function toggleAcc(btn){const acc=btn&&btn.closest?btn.closest('.acc'):null;if(acc)acc.classList.toggle('closed')}
function updatePromptClearButton(){const input=$('promptInput');const btn=$('promptClearBtn');if(!input||!btn)return;const has=(input.value||'').trim().length>0;btn.classList.toggle('hidden',!has)}
function bindPromptClearButton(){const input=$('promptInput');const btn=$('promptClearBtn');if(!input||!btn)return;input.addEventListener('input',updatePromptClearButton);btn.addEventListener('click',()=>{input.value='';updatePromptClearButton();input.focus()});updatePromptClearButton();}

// Plynulý „teploměr“ progressu: reálné hodnoty z workeru necháváme jako cíl,
// ale vizuální indikátor se mezi skoky dopočítává a animuje plynule.
const SMOOTH_LIVE_STATUSES=['pending','processing','queued','generating','uploading','downloading'];
const smoothProgress=new Map();
function clampProgress(v){v=Number(v);if(!Number.isFinite(v))v=0;return Math.max(0,Math.min(100,v));}
function isSmoothLive(status){return SMOOTH_LIVE_STATUSES.includes(String(status||''));}
function ensureSmoothProgress(j){
  const id=+j.id;if(!id)return {visual:0,target:0,real:0,live:false,status:''};
  const now=Date.now();
  const real=clampProgress(j.progress);
  const status=String(j.status||'');
  const live=isSmoothLive(status);
  let st=smoothProgress.get(id);
  if(!st){st={visual:real,target:real,real:real,rate:0,lastTargetAt:now,live,status};smoothProgress.set(id,st);return st;}
  if(real!==st.real||status!==st.status){
    const dt=Math.max(1,(now-(st.lastTargetAt||now))/1000);
    const jump=real-st.real;
    if(jump>0&&live){const r=jump/dt;st.rate=st.rate?st.rate*.65+r*.35:r;}
    st.real=real;st.target=real;st.lastTargetAt=now;st.status=status;st.live=live;
    if(!live||status==='done'||status==='error'||status==='cancelled')st.visual=real;
    if(real<st.visual-3)st.visual=real;
  }else{st.live=live;st.status=status;}
  if(!live){st.visual=real;st.target=real;st.real=real;}
  return st;
}
function smoothProgressValue(j){return Math.round(ensureSmoothProgress(j).visual);}
function smoothTarget(st,now){
  let target=st.target;
  // Jemný odhad mezi skutečnými updaty, aby bar nestál úplně mrtvě.
  // Držíme tvrdý strop, aby se nikdy netvářil jako hotový předčasně.
  if(st.live&&target>0&&target<98&&st.rate>0){
    const elapsed=(now-(st.lastTargetAt||now))/1000;
    target=Math.min(98,target+Math.min(6,st.rate*elapsed*.45));
  }
  return clampProgress(target);
}
function updateSmoothProgressDom(id,pct){
  const safe=clampProgress(pct);
  document.querySelectorAll(`[data-pz-progress="${id}"]`).forEach(el=>{el.style.width=safe.toFixed(1)+'%'});
  document.querySelectorAll(`[data-pz-progress-text="${id}"]`).forEach(el=>{el.textContent=Math.round(safe)+'%'});
}
function smoothProgressTick(){
  const now=Date.now();
  smoothProgress.forEach((st,id)=>{
    const target=smoothTarget(st,now);
    if(!st.live){st.visual=st.target;}
    else{
      const diff=target-st.visual;
      if(Math.abs(diff)<.05)st.visual=target;
      else st.visual+=diff*.08;
    }
    updateSmoothProgressDom(id,st.visual);
  });
  requestAnimationFrame(smoothProgressTick);
}
requestAnimationFrame(smoothProgressTick);
function toggleSide(v){$('side').classList.toggle('open',v);$('mask').classList.toggle('open',v)}
let apiBackoffUntil=0;try{localStorage.removeItem('pz_comfy_429_until');}catch(e){}
let bulkUploadActive=false;
const BULK_UPLOAD_DELAY_MS=4000;
function mark429(){/* LAN rezim: bez hostingoveho backoffu a bez chipu */}
function isAuthExpiredResponse(d){const msg=String(d&&d.error||'').toLowerCase();return !!(d&&(d.auth_expired||+d.http_status===401||(+d.http_status===403&&msg.includes('token'))||msg.includes('nepřihl')||msg.includes('neprihl')||msg.includes('relace')))}
function saveReturnJob(id){try{if(id)sessionStorage.setItem('pz_comfy_return_job',String(id));}catch(e){}}
function authExpiredBox(id){return `<div class="detail-empty" style="gap:12px;color:#ffb4b4"><div><b>Relace vypršela.</b><br><span class="small">Render může dál běžet nebo už být hotový. Přihlas se znovu a detail jobu se znovu načte.</span></div><button class="btn primary" onclick="saveReturnJob(${id?+id:0});location.reload()">Přihlásit znovu</button></div>`}
function handleAuthExpired(id=0){authExpired=true;if(typeof showPinGate==='function'&&PIN_REQUIRED)showPinGate();saveReturnJob(id||selectedId||0);const st=$('stats');if(st)st.innerHTML='<span class="chip bad">Relace vypršela · přihlas se znovu</span>';return true}
function tryRestoreReturnJob(){if(restoreJobDone)return;let id=0;try{id=parseInt(sessionStorage.getItem('pz_comfy_return_job')||'0',10)||0}catch(e){}if(!id)return;if(!jobs.some(j=>+j.id===+id))return;restoreJobDone=true;try{sessionStorage.removeItem('pz_comfy_return_job')}catch(e){}selectedId=id;selectedJobs.clear();selectedJobs.add(id);renderJobs();loadDetail(id)}
async function api(action,method='GET',body=null){
  const opt={method,credentials:'same-origin',headers:{'X-CSRF-Token':CSRF}};
  if(body&&!(body instanceof FormData)){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}else if(body){opt.body=body}
  if(Date.now()<apiBackoffUntil && !action.startsWith('create_job')){
    return {success:false,error:'Server dočasně odmítá požadavky. Zkus to za chvíli.'};
  }
  let r,txt;
  try{r=await fetch(`${API}?action=${action}`,opt);txt=await r.text();}
  catch(e){return {success:false,error:'Síťová chyba: '+e.message};}
  let d;
  try{d=txt?JSON.parse(txt):{};}
  catch(e){
    const html=String(txt||'').trim().startsWith('<');
    d={success:false,error:r.status===429||html?'Server dočasně odmítá požadavky (429). Počkej chvíli a zkus znovu.':'Server vrátil nečitelnou odpověď',raw:txt};
  }
  d.http_status=r.status;
  if(isAuthExpiredResponse(d)){d.success=false;d.auth_expired=true;handleAuthExpired(selectedId||0);}
  if(r.status===429 || String(txt||'').trim().toLowerCase().startsWith('<!doctype')){
    mark429();
    d.success=false;
    d.error=d.error||'Moc požadavků (429). Počkej chvíli.';
  }
  return d;
}

const POLL_CHANNEL='pz_comfy_dashboard_v3';
const TAB_ID=String(Date.now())+'_'+Math.random().toString(16).slice(2);
const pollBC=('BroadcastChannel' in window)?new BroadcastChannel(POLL_CHANNEL):null;
function isPollLeader(){
  const key='pz_comfy_poll_leader_v3';
  const now=Date.now();
  try{
    const raw=localStorage.getItem(key);
    const cur=raw?JSON.parse(raw):null;
    if(!cur||!cur.expires||cur.expires<now||cur.id===TAB_ID){
      localStorage.setItem(key,JSON.stringify({id:TAB_ID,expires:now+45000}));
      return true;
    }
  }catch(e){return true;}
  return false;
}
function broadcastDashboard(d){try{if(pollBC&&d&&d.success)pollBC.postMessage({type:'dashboard',detail_id:selectedId||0,data:d});}catch(e){}}
function applyDashboardPayload(d){
  if(!d||!d.success)return;
  applyJobsData(d);
  renderStatsData(d);
  tryRestoreReturnJob();
  if(d.detail&&selectedId&&+d.detail.job.id===+selectedId){detailCache=d.detail;renderDetail(d.detail.job,d.detail.events||[])}
  else if(selectedId&&d.detail_error){if(isAuthExpiredResponse({error:d.detail_error,http_status:d.http_status})){handleAuthExpired(selectedId);$('detail').innerHTML=authExpiredBox(selectedId)}else{$('detail').innerHTML=`<div class="err">${esc(d.detail_error)}</div>`;}}
}
if(pollBC){pollBC.onmessage=e=>{const m=e.data||{};if(m.type==='dashboard'&&m.data&&m.data.success){let d=m.data;if(m.detail_id&&selectedId&&+m.detail_id!==+selectedId){d={...m.data};delete d.detail;delete d.detail_error;}applyDashboardPayload(d);}};}

function setPromptLang(lang){promptLang=(appLang==='en')?'en':(lang==='en'?'en':'cs');if($('langCsBtn'))$('langCsBtn').classList.toggle('active',promptLang==='cs');if($('langEnBtn'))$('langEnBtn').classList.toggle('active',promptLang==='en');if($('langCsBtn'))$('langCsBtn').setAttribute('aria-pressed',promptLang==='cs'?'true':'false');if($('langEnBtn'))$('langEnBtn').setAttribute('aria-pressed',promptLang==='en'?'true':'false');if($('langHint'))$('langHint').textContent=promptLang==='cs'?ui('CZ → EN automaticky na pozadí','Czech → English translation enabled'):ui('EN bez překladu','English direct, no translation');if($('translationPreview')){$('translationPreview').className='lang-status';$('translationPreview').textContent=''}}
function clearPreviewUrls(){if(currentPreviewUrl){try{URL.revokeObjectURL(currentPreviewUrl)}catch(e){} currentPreviewUrl=null}for(const u of currentPreviewUrls){try{URL.revokeObjectURL(u)}catch(e){}}currentPreviewUrls=[]}
function imageEmptyPlaceholder(){return '<div class="empty-upload"><div class="empty-upload-icon">＋</div><div>'+ui('Vybrat nebo přetáhnout obrázek','Select or drag an image')+'</div><small>JPG / PNG / WEBP</small></div>'}
function image2EmptyPlaceholder(){return '<div class="empty-upload"><div class="empty-upload-icon">＋</div><div>'+ui('Vybrat poslední frejm','Select last frame')+'</div><small>JPG / PNG / WEBP</small></div>'}
function targetAspectRatio(){const w=Math.max(1,+($('width')?.value||1920));const h=Math.max(1,+($('height')?.value||1080));return w/h}
function getCropSlot(which){return which===2?{box:$('imgPreview2'),fileEl:$('imageInput2'),getFile:()=>selectedImageFile2,setCrop:c=>imageCrop2=c,getCrop:()=>imageCrop2}:{box:$('imgPreview'),fileEl:$('imageInput'),getFile:()=>selectedImageFiles[0]||null,setCrop:c=>imageCrop1=c,getCrop:()=>imageCrop1}}
function openPickerForCrop(which){if(which===2)openImagePicker2();else openImagePicker()}
function ratioFrameHtml(src,alt,note='Rámeček ukazuje výsledný poměr stran videa'){return `<div class="preview-stage" data-crop-stage="1"><img class="crop-img" src="${src}" alt="${alt}" draggable="false"><div class="ratio-frame"></div></div><div class="ratio-note">${note}</div>`}
function computeFrameRect(stage){const sw=stage.clientWidth||stage.parentElement?.clientWidth||300;const sh=stage.clientHeight||stage.parentElement?.clientHeight||260;const pad=12;const ratio=targetAspectRatio();let fw=sw-pad*2,fh=fw/ratio;if(fh>sh-pad*2){fh=sh-pad*2;fw=fh*ratio}return{x:(sw-fw)/2,y:(sh-fh)/2,w:fw,h:fh}}
function clampCrop(c){if(!c)return c;c.displayW=c.naturalW*c.scale;c.displayH=c.naturalH*c.scale;const minX=c.frameX+c.frameW-c.displayW;const maxX=c.frameX;const minY=c.frameY+c.frameH-c.displayH;const maxY=c.frameY;c.x=Math.min(maxX,Math.max(minX,c.x));c.y=Math.min(maxY,Math.max(minY,c.y));return c}
function updateCropDom(which,forceFit=false){const slot=getCropSlot(which);const box=slot.box;if(!box)return;const c=slot.getCrop();const stage=box.querySelector('.preview-stage');const img=box.querySelector('.crop-img');const frame=box.querySelector('.ratio-frame');if(!c||!stage||!img||!frame)return;const r=computeFrameRect(stage);const prev={...c};const targetRatio=targetAspectRatio();c.frameX=r.x;c.frameY=r.y;c.frameW=r.w;c.frameH=r.h;const minScale=Math.max(r.w/c.naturalW,r.h/c.naturalH);const ratioChanged=Math.abs((prev.targetRatio||0)-targetRatio)>0.0001;const mustRefit=forceFit||ratioChanged||!prev.scale;if(mustRefit){c.scale=minScale;c.x=r.x+(r.w-c.naturalW*c.scale)/2;c.y=r.y+(r.h-c.naturalH*c.scale)/2}else{const oldScale=prev.scale||minScale;const centerImgX=((prev.frameX+prev.frameW/2)-prev.x)/oldScale;const centerImgY=((prev.frameY+prev.frameH/2)-prev.y)/oldScale;c.scale=Math.max(prev.scale,minScale);c.x=r.x+r.w/2-centerImgX*c.scale;c.y=r.y+r.h/2-centerImgY*c.scale}c.targetRatio=targetRatio;clampCrop(c);slot.setCrop(c);frame.style.left=c.frameX+'px';frame.style.top=c.frameY+'px';frame.style.width=c.frameW+'px';frame.style.height=c.frameH+'px';img.style.width=c.displayW+'px';img.style.height=c.displayH+'px';img.style.transform='translate('+c.x+'px,'+c.y+'px)'}
function refreshAspectFrames(forceFit=false){[1,2].forEach(which=>{const slot=getCropSlot(which);const box=slot.box;if(box&&box.querySelector('.ratio-frame'))updateCropDom(which,forceFit)})}
function refitAspectFramesAfterResolutionChange(){refreshAspectFrames(true)}
window.addEventListener('resize',()=>setTimeout(()=>refreshAspectFrames(false),0));
function initCropPreview(which,url,alt){const slot=getCropSlot(which);const box=slot.box;if(!box)return;box.classList.remove('empty-preview');box.classList.add('with-ratio-frame');slot.setCrop(null);box.innerHTML=ratioFrameHtml(url,alt,'Rámeček je výsledný záběr. Fotku můžeš táhnout a kolečkem myši přiblížit.');const stage=box.querySelector('.preview-stage');const img=box.querySelector('.crop-img');img.onload=()=>{slot.setCrop({naturalW:img.naturalWidth||img.width||1,naturalH:img.naturalHeight||img.height||1,x:0,y:0,scale:0,frameX:0,frameY:0,frameW:0,frameH:0,displayW:0,displayH:0});updateCropDom(which)};if(img.complete&&(img.naturalWidth||img.width))img.onload();stage.addEventListener('pointerdown',e=>{const c=slot.getCrop();if(!c)return;cropDragState={which,id:e.pointerId,startX:e.clientX,startY:e.clientY,origX:c.x,origY:c.y,moved:false};stage.classList.add('dragging');try{stage.setPointerCapture(e.pointerId)}catch(_e){}e.preventDefault();e.stopPropagation()});stage.addEventListener('pointermove',e=>{if(!cropDragState||cropDragState.which!==which||cropDragState.id!==e.pointerId)return;const c=slot.getCrop();if(!c)return;const dx=e.clientX-cropDragState.startX,dy=e.clientY-cropDragState.startY;if(Math.abs(dx)+Math.abs(dy)>5)cropDragState.moved=true;c.x=cropDragState.origX+dx;c.y=cropDragState.origY+dy;clampCrop(c);slot.setCrop(c);const im=box.querySelector('.crop-img');if(im){im.style.width=c.displayW+'px';im.style.height=c.displayH+'px';im.style.transform='translate('+c.x+'px,'+c.y+'px)'}e.preventDefault();e.stopPropagation()});const stop=e=>{if(!cropDragState||cropDragState.which!==which||cropDragState.id!==e.pointerId)return;const wasClick=!cropDragState.moved;cropDragState=null;stage.classList.remove('dragging');e.preventDefault();e.stopPropagation();if(wasClick)openPickerForCrop(which)};stage.addEventListener('pointerup',stop);stage.addEventListener('pointercancel',e=>{cropDragState=null;stage.classList.remove('dragging');e.preventDefault();e.stopPropagation()});stage.addEventListener('wheel',e=>{const c=slot.getCrop();if(!c)return;e.preventDefault();e.stopPropagation();const dy=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?100:1);const minScale=Math.max(c.frameW/c.naturalW,c.frameH/c.naturalH);const maxScale=minScale*8;const next=Math.min(maxScale,Math.max(minScale,c.scale*Math.exp(-dy*0.0015)));if(!Number.isFinite(next)||next===c.scale)return;const r=stage.getBoundingClientRect();const px=e.clientX-r.left,py=e.clientY-r.top;const k=next/c.scale;c.x=px-(px-c.x)*k;c.y=py-(py-c.y)*k;c.scale=next;clampCrop(c);slot.setCrop(c);const im=box.querySelector('.crop-img');if(im){im.style.width=c.displayW+'px';im.style.height=c.displayH+'px';im.style.transform='translate('+c.x+'px,'+c.y+'px)'}},{passive:false});}
function renderImagePreview2(file){const box=$('imgPreview2');if(!box)return;if(currentPreviewUrl2){try{URL.revokeObjectURL(currentPreviewUrl2)}catch(e){}currentPreviewUrl2=null}if(!file){box.classList.add('empty-preview');box.classList.remove('with-ratio-frame');imageCrop2=null;box.innerHTML=image2EmptyPlaceholder();return}currentPreviewUrl2=URL.createObjectURL(file);initCropPreview(2,currentPreviewUrl2,'last frame')}
function renderImagePreview(files){const arr=Array.from(files||[]);const box=$('imgPreview');if(!arr.length){box.classList.add('empty-preview');box.classList.remove('with-ratio-frame');imageCrop1=null;box.innerHTML=imageEmptyPlaceholder();return}box.classList.remove('empty-preview');clearPreviewUrls();if(arr.length===1){currentPreviewUrl=URL.createObjectURL(arr[0]);initCropPreview(1,currentPreviewUrl,'preview');return}box.classList.remove('with-ratio-frame');imageCrop1=null;const shown=arr.slice(0,60);currentPreviewUrls=shown.map(f=>URL.createObjectURL(f));const cards=shown.map((f,i)=>`<div class="preview-card"><img src="${currentPreviewUrls[i]}" loading="lazy" alt="${esc(f.name)}"><div class="name" title="${esc(f.name)}">${esc(f.name)}</div></div>`).join('');const more=arr.length>shown.length?`<div class="preview-badge">+ ${arr.length-shown.length} dalších fotek se v náhledu nenačítá kvůli paměti prohlížeče</div>`:'';box.innerHTML=`<div style="width:100%"><div class="preview-badge">Vybráno obrázků: <b>${arr.length}</b></div><div class="preview-grid" style="margin-top:12px">${cards}</div>${more}</div>`}
async function makeCroppedFile(which,file,settings){const slot=getCropSlot(which);const c=slot.getCrop();if(!file||!c)return file;const srcImg=new Image();const url=URL.createObjectURL(file);try{await new Promise((res,rej)=>{srcImg.onload=res;srcImg.onerror=rej;srcImg.src=url});clampCrop(c);const sx=Math.max(0,(c.frameX-c.x)/c.scale);const sy=Math.max(0,(c.frameY-c.y)/c.scale);const sw=Math.min(c.naturalW-sx,c.frameW/c.scale);const sh=Math.min(c.naturalH-sy,c.frameH/c.scale);const canvas=document.createElement('canvas');canvas.width=Math.max(256,+settings.width||1920);canvas.height=Math.max(256,+settings.height||1080);const ctx=canvas.getContext('2d');ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';ctx.drawImage(srcImg,sx,sy,sw,sh,0,0,canvas.width,canvas.height);const mime=(file.type&&/^image\/(jpeg|png|webp)$/.test(file.type))?file.type:'image/png';const blob=await new Promise(resolve=>canvas.toBlob(resolve,mime,0.95));if(!blob)return file;return new File([blob],file.name,{type:mime,lastModified:Date.now()})}catch(e){console.warn('Crop selhal, posílám původní soubor',e);return file}finally{try{URL.revokeObjectURL(url)}catch(e){}}}
function normalizeImageFiles(list){const all=Array.from(list||[]);const ok=['image/jpeg','image/png','image/webp'];const filtered=all.filter(file=>ok.includes(file.type)||/\.(jpg|jpeg|png|webp)$/i.test(file.name||''));if(!filtered.length){alert('Podporované jsou jen JPG, PNG a WEBP.');return []}if(filtered.length!==all.length)alert('Některé soubory byly přeskočeny. Podporované jsou jen JPG, PNG a WEBP.');return filtered}
function loadImageDimensionsFromFile(file){return new Promise(async(resolve,reject)=>{if(!file){reject(new Error('Obrázek nejde načíst'));return}try{if(window.createImageBitmap){const bmp=await createImageBitmap(file);const out={width:bmp.width||0,height:bmp.height||0};try{bmp.close&&bmp.close()}catch(e){}if(out.width>0&&out.height>0){resolve(out);return}}}catch(e){}const url=URL.createObjectURL(file);const img=new Image();img.onload=()=>{const out={width:img.naturalWidth||img.width||0,height:img.naturalHeight||img.height||0};try{URL.revokeObjectURL(url)}catch(e){}if(out.width>0&&out.height>0)resolve(out);else reject(new Error('Obrázek nejde načíst'))};img.onerror=()=>{try{URL.revokeObjectURL(url)}catch(e){}reject(new Error('Obrázek nejde načíst'))};img.src=url})}
function fallbackAutoSize(){const w=(+$('width').value)||1920;const h=(+$('height').value)||1080;return {w:Math.max(256,w),h:Math.max(256,h)}}
// LTX 2.3: rozměr musí mít sudý podíl po dělení 32 (viz workflow.ltx_safe_size
// na backendu). Auto formát dřív zaokrouhloval na osmičky a půlka fotek tak
// vyrobila rozlišení, na kterém render spadl na nesouhlasu tenzorů.
function ltxSafeSize(v){v=Math.round(+v||0);v=Math.max(256,Math.min(4096,v));if(Math.floor(v/32)%2===0)return v;return Math.max(256,Math.min(4096,Math.round(v/64)*64))}
function calcAutoFhdSize(srcW,srcH){srcW=+srcW||0;srcH=+srcH||0;if(!srcW||!srcH)return{w:1920,h:1088};const landscape=srcW>=srcH;const maxW=landscape?1920:1088;const maxH=landscape?1088:1920;let scale=Math.min(maxW/srcW,maxH/srcH);if(!Number.isFinite(scale)||scale<=0)scale=1;let w=ltxSafeSize(srcW*scale);let h=ltxSafeSize(srcH*scale);if(w>maxW)w=ltxSafeSize(maxW);if(h>maxH)h=ltxSafeSize(maxH);return{w,h}}
async function applyAutoFormatFromFile(file,targetPrefix=''){if(!file)return false;try{const dims=await loadImageDimensionsFromFile(file);const out=calcAutoFhdSize(dims.width,dims.height);const wEl=targetPrefix?$(targetPrefix+'Width'):$('width');const hEl=targetPrefix?$(targetPrefix+'Height'):$('height');if(wEl)wEl.value=out.w;if(hEl)hEl.value=out.h;if(targetPrefix){const fmtEl=$(targetPrefix+'Format');if(fmtEl)fmtEl.value='auto_fhd';}else{const fmtEl=$('format');if(fmtEl)fmtEl.value='auto_fhd';updateMobSummary();}return true}catch(e){console.warn('Auto formát selhal',e);return false}}
async function setImageFiles(files){const valid=normalizeImageFiles(files);if(!valid.length)return;selectedImageFiles=valid;try{const dt=new DataTransfer();valid.forEach(f=>dt.items.add(f));$('imageInput').files=dt.files}catch(e){}if(($('format')?.value||'')==='auto_fhd')await applyAutoFormatFromFile(valid[0]);renderImagePreview(valid);refreshAspectFrames()}
async function setImageFile2(files){const valid=normalizeImageFiles(files);if(!valid.length)return;selectedImageFile2=valid[0];try{const dt=new DataTransfer();dt.items.add(selectedImageFile2);$('imageInput2').files=dt.files}catch(e){}renderImagePreview2(selectedImageFile2);refreshAspectFrames()}
async function translateTextStrict(text,source='cs',target='en'){const d=await api('translate_prompt','POST',{text,source,target});if(!d.success||!d.translated||!String(d.translated).trim())throw new Error(d.error||'Překlad selhal');return{text:String(d.translated).trim(),provider:d.provider||'unknown'}}
function formatRangeDisplay(id,val){const n=Number(val);if(!Number.isFinite(n))return String(val??'');if(id==='steps')return String(Math.round(n));if(id==='cfg')return n.toFixed(1).replace('.',',');if(id==='motion')return n.toFixed(2).replace(/0$/,'').replace(/\.$/,'').replace('.',',');if(id==='enhanceTokens')return String(Math.round(n));return String(val)}
function syncRangeValue(id){const input=$(id),out=$(id+'Val');if(!input||!out)return;out.textContent=formatRangeDisplay(id,input.value)}
function syncAllRangeValues(){['steps','cfg','motion','enhanceTokens'].forEach(syncRangeValue)}
/* ============================================================
   PRESETY pro LTX-2.3 image-to-video
   ============================================================
   Camera motion presety: konkrétní formulace pohybů kamery, formulované
   tak aby s LTX-2.3 fungovaly stabilně. Každý preset:
   - používá natural language (LTX-2.3 nemá rád číselné specifikace)
   - dává JEDEN pohyb (Lightricks doporučuje nestackovat pohyby — způsobuje jitter)
   - obsahuje stabilizační vodítka ("stabilized", "smooth", "continuous")
   - je formulován z pohledu kamery, ne abstraktního stylu
   POZN: Klíče slovníku jsou české labely z dropdownu — texty posílané do LTX
   zůstávají anglicky (model rozumí jen EN).
   Style presety: estetika scény (look, lighting, color grading), nezávislá na pohybu.
   Source: ltx.io/model/model-blog/ltx-2-3-prompt-guide + komunita.
*/
function cameraPresetText(p){return {
"Decentní nájezd dopředu":"the camera pushes in only slightly toward the subject in a restrained and minimal slow dolly forward, the framing tightens just a touch over the duration, smooth, stabilized and continuous",
"Pomalý nájezd dopředu":"the camera slowly pushes in toward the subject in a smooth dolly forward, gradually tightening the framing, stabilized and continuous",
"Pomalý odjezd dozadu":"the camera slowly pulls back from the subject in a smooth dolly out, gradually revealing more of the surrounding environment, stabilized and continuous",
"Obíhání kolem objektu":"the camera circles slowly around the subject in a smooth orbital motion, the subject stays centered in frame, steady continuous parallax",
"Půlkruhový oblouk":"the camera arcs around the subject in a controlled half-circle, smooth and stabilized, gradually revealing the subject from a new angle",
"Stoupání kamery (dron nahoru)":"the camera rises upward in a smooth aerial drone movement, gradually revealing the wider landscape below, stabilized and continuous",
"Klesání kamery (pohled dolů)":"the camera descends slowly from a high overhead view looking straight down at the scene, smooth aerial motion, stabilized",
"Jeřáb nahoru":"the camera cranes upward in a slow controlled vertical rise, the subject remains in frame, smooth and continuous",
"Jeřáb dolů":"the camera cranes downward in a slow controlled vertical descent, smooth and stabilized, gradually framing the subject from a lower angle",
"Pomalý posun do strany":"the camera tracks slowly to the side in a smooth horizontal dolly parallel to the subject, stabilized and continuous",
"Statická kamera (stativ)":"the camera holds completely still on a locked-off tripod, no camera movement, only the subject and the environment evolve over time",
"Jemný posun (drobný drift)":"the camera drifts with very subtle, almost imperceptible motion, minimal parallax, breathing-like and stabilized",
"Z ruky (dokumentární)":"the camera follows in a natural handheld documentary style, slight organic motion, observational and credible, lightly stabilized but not locked"
}[p]||''}
function stylePresetText(p){return {
"None":"",
"Cinematic":"cinematic film look, shot on 35mm lens, shallow depth of field, soft dramatic lighting, rich color grading",
"Realistic":"realistic natural look, neutral color grading, balanced natural lighting, accurate proportions, photographic depth of field, sharp authentic detail",
"Documentary / News":"documentary news footage style, natural daylight, credible journalistic look, neutral colors, sharp realistic detail, broadcast quality",
"Fashion / Product":"luxury commercial product look, glossy highlights, controlled studio lighting, shallow depth of field, polished color grading, macro detail",
"Music video":"stylized music video aesthetic, dramatic contrast, vibrant color grading, cinematic atmosphere, expressive lighting"
}[p]||''}
function resolveCameraMotion(){
  const manual=($('camera')?.value||'').trim();
  const presetText=cameraPresetText($('preset')?.value||'').trim();
  // Při odeslání se camera motion nikdy nesmí ztratit: když je pole prázdné,
  // vezmeme text z vybraného presetového pohybu kamery.
  const finalText=manual||presetText;
  if(finalText&&$('camera')&&!manual)$('camera').value=finalText;
  return finalText;
}
// Když uživatel změní camera preset, přepíšeme pole "Camera motion".
// Pole je pořád editovatelné — uživatel si může text doupravit nebo přepsat.
// Používáme značku "_pz_dirty" abychom nepřepsali ručně upravený text.
$('preset').addEventListener('change',()=>{$('camera').value=cameraPresetText($('preset').value);$('camera').dataset.pzDirty='0'});
$('camera').addEventListener('input',()=>{$('camera').dataset.pzDirty='1'});
// Stejné pro Style — preset píše do pole "Style", uživatel může přepsat.
$('style').addEventListener('change',()=>{$('styleText').value=stylePresetText($('style').value);$('styleText').dataset.pzDirty='0'});
$('styleText').addEventListener('input',()=>{$('styleText').dataset.pzDirty='1'});
const formatPresets={hd_landscape:{w:1280,h:720},fhd_landscape:{w:1920,h:1088},hd_portrait:{w:720,h:1280},fhd_portrait:{w:1088,h:1920},square:{w:1024,h:1024},square_2000:{w:1984,h:1984},classic_4_3:{w:1472,h:1088},classic_3_4:{w:1088,h:1472}};
function computeAspect(w,h){if(!w||!h)return'custom';const r=w/h;if(Math.abs(r-16/9)<0.01)return'16:9';if(Math.abs(r-9/16)<0.01)return'9:16';if(Math.abs(r-4/3)<0.01)return'4:3';if(Math.abs(r-3/4)<0.01)return'3:4';if(Math.abs(r-1)<0.01)return'1:1';return'custom'}
function applyFormat(key){const f=formatPresets[key];if(!f)return;$('width').value=f.w;$('height').value=f.h}
function syncFormatFromSize(){const w=+$('width').value||0,h=+$('height').value||0;let format='custom';for(const [key,f] of Object.entries(formatPresets)){if(w===f.w&&h===f.h){format=key;break}}$('format').value=format;updateMobSummary()}
$('format').addEventListener('change',async()=>{const k=$('format').value;if(k==='auto_fhd'){const file=selectedImageFiles[0]||Array.from($('imageInput').files||[])[0];if(file)await applyAutoFormatFromFile(file);else{$('width').value=1920;$('height').value=1088;}}else if(k!=='custom')applyFormat(k);updateMobSummary();refitAspectFramesAfterResolutionChange();});$('width').addEventListener('input',()=>{syncFormatFromSize();refreshAspectFrames(true)});$('height').addEventListener('input',()=>{syncFormatFromSize();refreshAspectFrames(true)});
['width','height'].forEach(id=>{const el=$(id);if(el)el.addEventListener('change',()=>{const v=ltxSafeSize(el.value);if(+el.value!==v)el.value=v;syncFormatFromSize();refreshAspectFrames(true)})});['steps','cfg','motion','enhanceTokens'].forEach(id=>{const el=$(id);if(el){el.addEventListener('input',()=>syncRangeValue(id));el.addEventListener('change',()=>syncRangeValue(id))}});['seed','seedMode','fps','duration'].forEach(id=>{const el=$(id);if(el){el.addEventListener('input',updateMobSummary);el.addEventListener('change',updateMobSummary)}});syncAllRangeValues();freshSeed();updateMobSummary();loadProjects();
function resetForm(){document.getElementById('jobForm').reset();selectedImageFiles=[];selectedImageFile2=null;imageCrop1=null;imageCrop2=null;cropDragState=null;try{$('imageInput').value='';$('imageInput2').value=''}catch(e){}clearPreviewUrls();renderImagePreview([]);renderImagePreview2(null);setPromptLang(appLang==='cs'?'cs':'en');$('format').value='auto_fhd';$('width').value=1920;$('height').value=1088;if($('promptEnhance'))$('promptEnhance').checked=false;if($('enhanceTokens'))$('enhanceTokens').value=128;if($('seedMode'))$('seedMode').value='increment_batch';syncAllRangeValues();updateMobSummary();updatePromptClearButton();adaptFormToProject(getSelectedProject())}
$('imageInput').addEventListener('change',e=>{const files=Array.from(e.target.files||[]);if(!files.length)return;setImageFiles(files)});
$('imageInput2')&&$('imageInput2').addEventListener('change',e=>{const files=Array.from(e.target.files||[]);if(!files.length)return;setImageFile2(files)});
['imgPreview','imgPreview2'].forEach(id=>{const el=$(id);if(!el)return;['dragenter','dragover'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.classList.add('dragover')}));['dragleave','dragend'].forEach(ev=>el.addEventListener(ev,e=>{e.preventDefault();e.stopPropagation();el.classList.remove('dragover')}));el.addEventListener('drop',e=>{e.preventDefault();e.stopPropagation();el.classList.remove('dragover');const files=e.dataTransfer&&e.dataTransfer.files;if(files&&files.length){if(id==='imgPreview2')setImageFile2(files);else setImageFiles(files)}})});
// Drag & drop na CELOU stránku: bez globálního preventDefault prohlížeč při dopadu
// mimo náhledové pole obrázek prostě otevře a odejde z aplikace. Drop kamkoliv
// mimo druhé pole (2 PICT) se bere jako vstupní obrázek.
['dragover','drop'].forEach(ev=>window.addEventListener(ev,e=>{e.preventDefault()}));
window.addEventListener('dragover',e=>{const p=$('imgPreview');if(p&&e.dataTransfer&&Array.from(e.dataTransfer.types||[]).includes('Files'))p.classList.add('dragover')});
window.addEventListener('dragleave',e=>{if(!e.relatedTarget){const p=$('imgPreview');if(p)p.classList.remove('dragover')}});
window.addEventListener('drop',e=>{
  const p=$('imgPreview');if(p)p.classList.remove('dragover');
  const files=e.dataTransfer&&e.dataTransfer.files;
  if(!files||!files.length)return;
  const t=e.target;
  if(t&&t.closest&&(t.closest('#imgPreview')||t.closest('#imgPreview2')))return; // řeší vlastní handler
  setImageFiles(files);
});
function openImagePicker(){const input=$('imageInput');if(!input)return;try{input.value=''}catch(e){}input.click()}
function openImagePicker2(){const input=$('imageInput2');if(!input)return;try{input.value=''}catch(e){}input.click()}
$('imgPreview').addEventListener('click',e=>{if(selectedImageFiles.length)return;e.preventDefault();e.stopPropagation();openImagePicker()});
$('imgPreview2')&&$('imgPreview2').addEventListener('click',e=>{if(selectedImageFile2)return;e.preventDefault();e.stopPropagation();openImagePicker2()});
$('imgPreview').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openImagePicker()}});$('imgPreview').setAttribute('tabindex','0');if($('imgPreview2')){$('imgPreview2').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openImagePicker2()}});$('imgPreview2').setAttribute('tabindex','0');}
function buildJobFormData(file,promptText,negativeText,settings,image2Override=null){const fd=new FormData();fd.append('image',file,file.name);const second=image2Override||selectedImageFile2;if(isCurrentTwoPict()&&second)fd.append('image2',second,second.name);fd.append('prompt',promptText);fd.append('negative_prompt',negativeText||'');fd.append('preset',$('preset').value||'Custom');fd.append('style',$('style').value||'None');fd.append('settings_json',JSON.stringify(settings));return fd}
function waitMs(ms){return new Promise(resolve=>setTimeout(resolve,ms))}
function shouldRetryCreateJob(d){const msg=String(d?.error||'').toLowerCase();const st=+(d?.http_status||0);if(st===429||st===403||st===401)return false;if(msg.includes('rate limit')||msg.includes('nepovolený typ')||msg.includes('moc velký')||msg.includes('prompt je prázdný'))return false;return st>=500||st===0||msg.includes('síťová')||msg.includes('nečitelnou')||msg.includes('nelze uložit obrázek')}
async function createJobWithRetry(file,finalPrompt,finalNegative,settings,image2Override=null){let last=null;for(let attempt=1;attempt<=2;attempt++){const fd=buildJobFormData(file,finalPrompt,finalNegative,settings,image2Override);fd.append('target_worker',selectedWorker);const _pid=document.getElementById('selectedProjectId')?.value||'';if(_pid)fd.append('project_id',_pid);const d=await api('create_job','POST',fd);if(d.success)return d;last=d;if(attempt<2&&shouldRetryCreateJob(d)){await waitMs(900);continue}break}return last||{success:false,error:'neznámá chyba'}}
const BULK_BATCH_SIZE=8;
async function createJobsBatchWithRetry(fileBatch,finalPrompt,finalNegative,settingsBatch,image2Override=null){if(isCurrentTwoPict()){const r=await createJobWithRetry(fileBatch[0],finalPrompt,finalNegative,(settingsBatch||[])[0]||{},image2Override);return r&&r.success?{...r,ids:[r.id],created_count:1,failed:[]}:r}let last=null;for(let attempt=1;attempt<=2;attempt++){const fd=new FormData();fileBatch.forEach(f=>fd.append('images[]',f,f.name));fd.append('prompt',finalPrompt);fd.append('negative_prompt',finalNegative||'');fd.append('preset',$('preset').value||'Custom');fd.append('style',$('style').value||'None');fd.append('settings_jsons',JSON.stringify(settingsBatch||[]));fd.append('target_worker',selectedWorker);const _pid=document.getElementById('selectedProjectId')?.value||'';if(_pid)fd.append('project_id',_pid);const d=await api('create_jobs_batch','POST',fd);if(d.success)return d;last=d;if(attempt<2&&shouldRetryCreateJob(d)){await waitMs(1200);continue}break}return last||{success:false,error:'neznámá chyba dávky'}}
$('jobForm').addEventListener('submit',async e=>{
  e.preventDefault();
  if(Date.now()<apiBackoffUntil){alert('Server teď odmítá požadavky. Počkej chvíli a zkus render znovu.');return;}
  const files=selectedImageFiles.length?selectedImageFiles:Array.from($('imageInput').files||[]);
  const twoPict=isCurrentTwoPict();
  if(!files.length){alert('Vyber aspoň jeden obrázek.');return}
  if(twoPict&&files.length>1){alert('Režim 2 PICT bere jen jeden první frejm a jeden poslední frejm. Vyber jen jednu první fotku.');return}
  if(twoPict&&!selectedImageFile2){alert('Vyber ještě poslední frejm / druhý obrázek.');return}
  const num=v=>parseFloat(String(v).replace(',','.'));
  const originalPrompt=($('promptInput')?.value||'').trim();
  const originalNegative=($('negativeInput')?.value||'').trim();
  if(!originalPrompt){alert('Prompt je prázdný.');return}
  const status=$('translationPreview');
  const isAutoFormat=($('format')?.value||'')==='auto_fhd';
  const baseSeed=+$('seed').value||randomSeed();const baseSettings={width:+$('width').value,height:+$('height').value,fps:+$('fps').value,duration:num($('duration').value),seed:baseSeed,seed_mode:($('seedMode')?.value||'increment_batch'),steps:+$('steps').value,cfg:num($('cfg').value),motion_strength:num($('motion').value),prompt_enhance:!!$('promptEnhance')?.checked,enhance_tokens:+($('enhanceTokens')?.value||128),camera_motion:resolveCameraMotion(),style:$('styleText').value,aspect:computeAspect(+$('width').value,+$('height').value),translate_prompt:false,input_language:promptLang,input_mode:twoPict?'2pict':'1pict'};
  if(isCurrentPhotoEdit()){baseSettings.camera_motion='';baseSettings.style='';baseSettings.prompt_enhance=false;baseSettings.input_mode='photo_edit';}
  const btn=e.submitter;btn.disabled=true;const originalBtnText=btn.textContent;bulkUploadActive=true;
  try{
    let finalPrompt=originalPrompt,finalNegative=originalNegative,translationProvider='none';
    if(appLang==='cs'&&promptLang==='cs'){
      status.className='lang-status show';status.textContent='Překládám prompt na pozadí…';
      const trMain=await translateTextStrict(originalPrompt,'cs','en');finalPrompt=trMain.text;translationProvider=trMain.provider;
      if(originalNegative){status.textContent='Překládám negative prompt…';const trNeg=await translateTextStrict(originalNegative,'cs','en');finalNegative=trNeg.text}
      status.innerHTML='<b>Přeloženo:</b> '+esc(translationProvider)
    }else{status.className='lang-status';status.textContent=''}

    let createdIds=[];let failed=[];let warnings=[];let prepared=[];let uploadFiles=files.slice();let uploadImage2File=selectedImageFile2;
    for(let i=0;i<files.length;i++){
      const seedMode=baseSettings.seed_mode||'increment_batch';const seedVal=seedMode==='random_each'?randomSeed():(seedMode==='locked'?baseSettings.seed:Math.min(2147483647,Math.max(1,(+baseSettings.seed||randomSeed())+(files.length>1?i:0))));const settings={...baseSettings,seed:seedVal,translated:promptLang==='cs',translation_provider:translationProvider};
      if(isAutoFormat){
        try{const dims=await loadImageDimensionsFromFile(files[i]);const autoSize=calcAutoFhdSize(dims.width,dims.height);settings.width=autoSize.w;settings.height=autoSize.h;settings.aspect=computeAspect(autoSize.w,autoSize.h)}
        catch(err){const autoSize=fallbackAutoSize();settings.width=autoSize.w;settings.height=autoSize.h;settings.aspect=computeAspect(autoSize.w,autoSize.h);warnings.push(`${files[i].name}: auto formát se nepodařilo načíst, použil se náhradní rozměr ${autoSize.w}×${autoSize.h}`)}
      }
      settings.original_prompt=originalPrompt;settings.original_negative_prompt=originalNegative;
      prepared.push(settings);
    }
    if(files.length===1){
      status.className='lang-status show';status.innerHTML='<b>Připravuji výřez podle rámečku…</b>';
      uploadFiles[0]=await makeCroppedFile(1,files[0],prepared[0]);
      if(twoPict&&selectedImageFile2)uploadImage2File=await makeCroppedFile(2,selectedImageFile2,prepared[0]);
    }

    for(let start=0;start<files.length;start+=BULK_BATCH_SIZE){
      const end=Math.min(start+BULK_BATCH_SIZE,files.length);
      const batchFiles=uploadFiles.slice(start,end);
      const batchSettings=prepared.slice(start,end);
      btn.textContent=`Odesílám ${start+1}-${end}/${files.length}...`;
      status.className='lang-status show';
      status.innerHTML=`<b>Odesílám dávku:</b> ${start+1}-${end}/${files.length}<br><span class="small">Dávkový upload šetří FTP hosting a nezahltí API.</span>`;
      const d=await createJobsBatchWithRetry(batchFiles,finalPrompt,finalNegative,batchSettings,uploadImage2File);
      if(d.success){
        const ids=Array.isArray(d.ids)?d.ids:[];createdIds.push(...ids);
        if(Array.isArray(d.failed)) d.failed.forEach(x=>failed.push(`${x.name||'soubor'}: ${x.error||'chyba'}`));
      }else{
        // Starší server bez batch endpointu: nouzově spadneme na staré odesílání po jednom.
        if(String(d.http_status||'')==='400'||String(d.error||'').toLowerCase().includes('chybí vstupní obrázky')){
          for(let i=start;i<end;i++){const r=await createJobWithRetry(uploadFiles[i],finalPrompt,finalNegative,prepared[i],uploadImage2File);if(r.success)createdIds.push(r.id);else failed.push(`${files[i].name}: ${r.error||'chyba'}`);await waitMs(900)}
        }else{
          batchFiles.forEach(f=>failed.push(`${f.name}: ${d.error||'chyba dávky'}`));
        }
      }
      if(end<files.length){status.innerHTML=`<b>Zařazeno:</b> ${createdIds.length}/${files.length}<br><span class="small">Krátká pauza mezi dávkami…</span>`;await waitMs(1500)}
    }

    if(createdIds.length){selectedId=createdIds[createdIds.length-1]||null;freshSeed();updateMobSummary();await loadJobs();if(selectedId)await loadDetail(selectedId);toggleSide(false)}
    if(failed.length){
      const msg=`Zařazeno: ${createdIds.length}/${files.length}. Selhalo: ${failed.length}.\n\n${failed.slice(0,6).join('\n')}`;
      const warnHtml=warnings.length?('<br><span class="small">Pozn.: '+esc(warnings.slice(0,2).join(' | '))+'</span>'):'';
      status.className='lang-status show';status.innerHTML='<b>Část odeslání selhala.</b><br>'+esc(msg).replace(/\n/g,'<br>')+warnHtml;alert(msg);return
    }
    if(warnings.length){status.className='lang-status show';status.innerHTML=`<b>Hotovo.</b> Do fronty přidáno jobů: <b>${createdIds.length}</b><br><span class="small">Některé soubory použily náhradní auto rozměr, protože se nepodařilo přečíst jejich náhled.</span>`}
    else{status.className='lang-status show';status.innerHTML=`<b>Hotovo.</b> Do fronty přidáno jobů: <b>${createdIds.length}</b>`}
  }catch(err){status.className='lang-status show';status.innerHTML='<b>Odeslání selhalo.</b><br>'+esc(err.message||String(err));alert('Nepovedlo se zařadit job do fronty: '+(err.message||String(err)))}
  finally{bulkUploadActive=false;btn.disabled=false;btn.textContent=originalBtnText;schedulePoll(12000)}
});
function updateSelectionInfo(){const el=$('selInfo');if(el)el.textContent=ui('Označeno: ','Selected: ')+selectedJobs.size}
function canStopJobStatus(status){return ['pending','processing','queued','generating','uploading','downloading'].includes(String(status||''))}
function clearSelection(){selectedJobs.clear();updateSelectionInfo();renderJobs()}
function selectAllJobs(){jobs.forEach(j=>selectedJobs.add(+j.id));updateSelectionInfo();renderJobs()}
function selectFinishedJobs(){jobs.forEach(j=>{if(j.status==='done')selectedJobs.add(+j.id)});updateSelectionInfo();renderJobs()}
function downloadFinishedJobs(){selectFinishedJobs();downloadSelectedJobs()}
function newJobForm(){selectedId=null;detailCache=null;$('detailTitle').textContent=ui('Detail jobu','Job detail');$('detail').innerHTML='<div class="detail-empty">'+ui('Vyplň formulář a odešli video job.','Fill the form and submit a video job.')+'</div>';hideActionButtons();document.querySelector('#formPanel').scrollIntoView({behavior:'smooth'});renderJobs()}
/* ── Upozornění na dokončený render ───────────────────────
   Render trvá minuty, takže se u toho nedá sedět. Když job dojde,
   cinkne to a vyskočí systémová notifikace (pokud ji uživatel povolil). */
const NOTIFY_KEY='pzcomfy_notify';
function notifyEnabled(){return localStorage.getItem(NOTIFY_KEY)!=='0'}
function setNotifyEnabled(on){
  localStorage.setItem(NOTIFY_KEY,on?'1':'0');
  if(on&&'Notification'in window&&Notification.permission==='default')Notification.requestPermission();
  updateNotifyButton();
}
function toggleNotify(){setNotifyEnabled(!notifyEnabled())}
function updateNotifyButton(){
  const b=$('notifyToggle');if(!b)return;
  const on=notifyEnabled();
  b.textContent=on?'🔔':'🔕';
  b.title=on?ui('Upozornění na hotový render: zapnuto','Finished-render alerts: on')
            :ui('Upozornění na hotový render: vypnuto','Finished-render alerts: off');
  b.setAttribute('aria-pressed',on?'true':'false');
}
function playDing(){
  try{
    const Ctx=window.AudioContext||window.webkitAudioContext;if(!Ctx)return;
    const ctx=new Ctx();const now=ctx.currentTime;
    // Dvoutónové cinknutí — krátké, ať to v open officu nikoho neirituje.
    [[880,0],[1320,.12]].forEach(([f,t])=>{
      const o=ctx.createOscillator(),g=ctx.createGain();
      o.type='sine';o.frequency.value=f;
      g.gain.setValueAtTime(0,now+t);
      g.gain.linearRampToValueAtTime(.18,now+t+.02);
      g.gain.exponentialRampToValueAtTime(.0001,now+t+.32);
      o.connect(g);g.connect(ctx.destination);o.start(now+t);o.stop(now+t+.34);
    });
    setTimeout(()=>{try{ctx.close()}catch(e){}},900);
  }catch(e){}
}
function notifyFinishedJobs(oldJobs,newJobs){
  if(!notifyEnabled()||!Array.isArray(oldJobs)||!oldJobs.length)return;
  const was=new Map(oldJobs.map(j=>[+j.id,String(j.status||'')]));
  const done=[],failed=[];
  for(const j of newJobs){
    if(j.foreign)continue;                       // cizí joby uživatele nezajímají
    const before=was.get(+j.id);
    if(before===undefined||!LIVE_STATUSES.includes(before))continue;
    if(j.status==='done')done.push(j);
    else if(j.status==='error')failed.push(j);
  }
  if(!done.length&&!failed.length)return;
  playDing();
  if(!('Notification'in window)||Notification.permission!=='granted')return;
  const body=j=>((j.settings||{}).original_prompt||j.prompt||'').trim().slice(0,90);
  try{
    if(done.length===1)new Notification(ui('Video je hotové','Video is ready')+' · #'+done[0].id,{body:body(done[0]),tag:'pz'+done[0].id});
    else if(done.length>1)new Notification(ui('Hotovo videí: ','Videos ready: ')+done.length,{tag:'pzmulti'});
    for(const j of failed)new Notification(ui('Render spadl','Render failed')+' · #'+j.id,{body:String(j.error||'').slice(0,120),tag:'pzerr'+j.id});
  }catch(e){}
}

function applyJobsData(d){
  notifyFinishedJobs(jobs, d.jobs||[]);
  jobs=d.jobs||[];
  const valid=new Set(jobs.map(j=>+j.id));
  selectedJobs=new Set([...selectedJobs].filter(id=>valid.has(id)));
  if(selectedId&&!valid.has(+selectedId))selectedId=null;
  smoothProgress.forEach((_,id)=>{if(!valid.has(+id)&&+selectedId!==+id)smoothProgress.delete(id)});
  updateSelectionInfo();
  renderJobs();
}
async function loadJobs(){await refreshDashboard(true)}
function renderJobs(){const box=$('jobs');if(!box)return;if(!jobs.length){box.innerHTML='<div class="small" style="padding:12px">'+ui('Žádné joby.','No jobs.')+'</div>';updateSelectionInfo();return}box.innerHTML=jobs.map(j=>{const id=+j.id;const active=id===+selectedId?'active':'';const multi=selectedJobs.has(id)?'selected-multi':'';const canReuse='can-reuse';const pct=smoothProgressValue(j);const tip=ui('Klik otevře detail, čtvereček vpravo označí job','Click opens detail, square on the right selects the job');const displayPrompt=(((j.settings||{}).original_prompt)||j.prompt||'').trim();const aria=ui('Označit job','Select job');return`<div class="job ${active} ${multi} ${canReuse}" data-job-id="${id}" onclick="selectJob(event,${id})" oncontextmenu="return openJobContextMenu(event,${id})" title="${tip}"><div class="job-row"><div class="job-main"><div class="job-title">#${id} ${esc(displayPrompt)}</div><div class="job-meta"><span class="pill p-${esc(j.status)}">${esc(j.status)}</span><span data-pz-progress-text="${id}">${pct}%</span><span>${esc(j.created_at||'')}</span></div></div><div class="job-check" data-select-check="1" data-job-id="${id}" role="checkbox" aria-label="${aria}" aria-checked="${selectedJobs.has(id)?'true':'false'}" title="${aria}"></div></div></div>`}).join('');updateSelectionInfo()}
function ensureJobContextMenu(){let m=$('jobContextMenu');if(!m){m=document.createElement('div');m.id='jobContextMenu';m.className='job-context-menu';document.body.appendChild(m)}return m}
function hideJobContextMenu(){const m=$('jobContextMenu');if(m)m.style.display='none'}
function openJobContextMenuAt(x,y,id){const j=jobs.find(xj=>+xj.id===+id);if(!j)return false;const m=ensureJobContextMenu();const stopBtn=canStopJobStatus(j.status)?`<button type="button" onclick="cancelJob(${id})">${ui('Zastavit render','Stop render')}</button>`:'';m.innerHTML=`${stopBtn}<button type="button" onclick="requeueJobFromQueue(${id},false)">${ui('Zopakovat se stejným seedem','Repeat with same seed')}</button><button type="button" onclick="requeueJobFromQueue(${id},true)">${ui('Zopakovat s novým seedem','Repeat with new seed')}</button><button type="button" onclick="useJobSettingsFromQueue(${id})">${ui('Použít nastavení (prompt)','Use settings (prompt)')}</button><div class="hint">${ui('Job #','Job #')}${id} · ${ui('můžeš ho zastavit nebo znovu zařadit se stejným obrázkem a novým seedem.','you can stop it or requeue it with the same image and a new seed.')}</div>`;m.style.display='block';m.style.left='0px';m.style.top='0px';const pad=8;const w=m.offsetWidth||260;const h=m.offsetHeight||140;const vx=Math.max(pad,Math.min(x||pad,window.innerWidth-w-pad));const vy=Math.max(pad,Math.min(y||pad,window.innerHeight-h-pad));m.style.left=vx+'px';m.style.top=vy+'px';return false}
function openJobContextMenu(ev,id){const j=jobs.find(x=>+x.id===+id);if(!j)return true;ev.preventDefault();ev.stopPropagation();return openJobContextMenuAt(ev.clientX,ev.clientY,id)}
function setSelectIfExists(id,value,fallback){const el=$(id);if(!el)return;const val=(value??'').toString();const opts=Array.from(el.options||[]).map(o=>o.value);if(opts.includes(val))el.value=val;else if(fallback!==undefined&&opts.includes(fallback))el.value=fallback}
function inferStylePresetByText(text){const t=(text||'').trim();for(const opt of Array.from($('style').options||[])){if((stylePresetText(opt.value)||'').trim()===t)return opt.value}return 'None'}
async function useJobSettingsFromQueue(id){hideJobContextMenu();const d=await api(`job_detail&id=${id}`);if(!d.success||!d.job){alert(d.error||'Nastavení se nepodařilo načíst.');return}const j=d.job;const s=j.settings||{};if(j.project_id){selectProject(+j.project_id,null)}else if(String(s.input_mode||'').toLowerCase().includes('2')){setPictModeFTP('2')}else{setPictModeFTP('1')}const originalPrompt=(s.original_prompt||'').trim();const englishPrompt=(j.prompt||'').trim();const useOriginal=!!originalPrompt&&originalPrompt!==englishPrompt;const inputLang=(s.input_language||'').trim().toLowerCase();const useCs=(inputLang==='cs')||(!inputLang&&useOriginal);setPromptLang(useCs?'cs':'en');$('promptInput').value=useOriginal?originalPrompt:(englishPrompt||originalPrompt);const originalNegative=(s.original_negative_prompt||'').trim();const englishNegative=(j.negative_prompt||'').trim();$('negativeInput').value=(useOriginal&&originalNegative)?originalNegative:englishNegative;setSelectIfExists('preset',j.preset||'Vlastní','Vlastní');$('camera').value=s.camera_motion||cameraPresetText($('preset').value)||'';$('camera').dataset.pzDirty='1';$('styleText').value=s.style||'';$('styleText').dataset.pzDirty='1';setSelectIfExists('style',inferStylePresetByText(s.style||''),'None');if(s.width)$('width').value=s.width;if(s.height)$('height').value=s.height;if(s.fps)$('fps').value=s.fps;if(s.steps)$('steps').value=s.steps;if(s.cfg!==undefined)$('cfg').value=s.cfg;if(s.motion_strength!==undefined)$('motion').value=s.motion_strength;if(s.duration)$('duration').value=s.duration;if(s.seed)$('seed').value=s.seed;if($('seedMode'))$('seedMode').value=s.seed_mode||'increment_batch';if($('promptEnhance'))$('promptEnhance').checked=!!s.prompt_enhance;if($('enhanceTokens'))$('enhanceTokens').value=s.enhance_tokens||128;syncFormatFromSize();syncAllRangeValues();$('translationPreview').className='lang-status show';$('translationPreview').innerHTML=`<b>${ui('Použito nastavení z jobu','Settings used from job')} #${id}.</b> ${ui('Do formuláře se vrátil původní prompt v jazyce zadání. Obrázek ve formuláři zůstal beze změny.','The original prompt language was restored to the form. The image in the form was left unchanged.')}`;document.querySelector('#formPanel').scrollIntoView({behavior:'smooth',block:'start'});updatePromptClearButton();toggleSide(false)}
async function requeueJobFromQueue(id,newSeed=true){hideJobContextMenu();const d=await api('rerun_job','POST',{id,new_seed:!!newSeed});if(!d.success){alert(d.error||'Nepodařilo se zařadit opakovaný render.');return}selectedId=d.id||null;if(newSeed)freshSeed();updateMobSummary();await loadJobs();if(selectedId)await loadDetail(selectedId);$('translationPreview').className='lang-status show';$('translationPreview').innerHTML=`<b>${ui('Znovu zařazeno.','Queued again.')}</b> ${ui('Job','Job')} #${id} ${newSeed?ui('byl vložen znovu do fronty s novým seedem','was queued again with new seed'):ui('byl vložen znovu do fronty se stejným seedem','was queued again with same seed')} <b>${esc(d.seed||'')}</b>.`;toggleSide(false)}
let longPressTimer=null;let longPressTriggered=false;let longPressJobId=0;function clearJobLongPress(){if(longPressTimer){clearTimeout(longPressTimer);longPressTimer=null}}const jobsBox=$('jobs');if(jobsBox){jobsBox.addEventListener('touchstart',e=>{if(e.target.closest('[data-select-check]'))return;const card=e.target.closest('.job[data-job-id]');if(!card)return;clearJobLongPress();longPressTriggered=false;longPressJobId=+(card.dataset.jobId||0);const touch=e.touches&&e.touches[0]?e.touches[0]:null;const x=touch?touch.clientX:window.innerWidth/2;const y=touch?touch.clientY:window.innerHeight/2;longPressTimer=setTimeout(()=>{longPressTriggered=true;openJobContextMenuAt(x,y,longPressJobId);},520)},{passive:true});jobsBox.addEventListener('touchmove',clearJobLongPress,{passive:true});jobsBox.addEventListener('touchend',()=>{setTimeout(clearJobLongPress,0)},{passive:true});jobsBox.addEventListener('touchcancel',clearJobLongPress,{passive:true});jobsBox.addEventListener('click',e=>{if(longPressTriggered){e.preventDefault();e.stopPropagation();longPressTriggered=false;}},true)}
document.addEventListener('click',hideJobContextMenu);document.addEventListener('scroll',hideJobContextMenu,true);document.addEventListener('keydown',e=>{if(e.key==='Escape')hideJobContextMenu()});

let mobileSelectDragging=false;let mobileSelectMode='add';let mobileSelectPointerId=null;let mobileSelectTouched=new Set();let suppressJobClickUntil=0;
function setJobSelectedVisual(id){const card=document.querySelector(`.job[data-job-id="${id}"]`);if(card)card.classList.toggle('selected-multi',selectedJobs.has(+id));const check=document.querySelector(`.job-check[data-job-id="${id}"]`);if(check)check.setAttribute('aria-checked',selectedJobs.has(+id)?'true':'false')}
function applyMobileMultiSelect(id,mode){id=+id;if(!id||mobileSelectTouched.has(id))return;mobileSelectTouched.add(id);if(mode==='add')selectedJobs.add(id);else selectedJobs.delete(id);lastSelectedId=id;setJobSelectedVisual(id);updateSelectionInfo()}
function checkFromPoint(ev){const el=document.elementFromPoint(ev.clientX,ev.clientY);return el?el.closest('[data-select-check]'):null}
function beginMobileMultiSelect(ev,check){const id=+(check.dataset.jobId||0);if(!id)return;if(ev){ev.preventDefault();ev.stopPropagation()}hideJobContextMenu();mobileSelectDragging=true;mobileSelectPointerId=ev.pointerId;mobileSelectTouched=new Set();mobileSelectMode=selectedJobs.has(id)?'remove':'add';suppressJobClickUntil=Date.now()+650;if(check.setPointerCapture){try{check.setPointerCapture(ev.pointerId)}catch(_){}}applyMobileMultiSelect(id,mobileSelectMode)}
function endMobileMultiSelect(){if(!mobileSelectDragging)return;mobileSelectDragging=false;mobileSelectPointerId=null;mobileSelectTouched.clear();setTimeout(()=>{if(Date.now()>suppressJobClickUntil)suppressJobClickUntil=0},0)}
function toggleJobMultiSelect(ev,id){if(ev){ev.preventDefault();ev.stopPropagation()}hideJobContextMenu();mobileSelectTouched=new Set();mobileSelectMode=selectedJobs.has(+id)?'remove':'add';applyMobileMultiSelect(id,mobileSelectMode);suppressJobClickUntil=Date.now()+650}
function jobCheckPointerDown(ev,id){const check=ev?.target?.closest?.('[data-select-check]')||document.querySelector(`.job-check[data-job-id="${id}"]`);if(check)beginMobileMultiSelect(ev,check)}
function jobCheckPointerEnter(ev,id){if(!mobileSelectDragging)return;applyMobileMultiSelect(id,mobileSelectMode)}
if(jobsBox){jobsBox.addEventListener('pointerdown',e=>{const check=e.target.closest('[data-select-check]');if(check)beginMobileMultiSelect(e,check)},true);jobsBox.addEventListener('click',e=>{if(e.target.closest('[data-select-check]')||Date.now()<suppressJobClickUntil){e.preventDefault();e.stopPropagation()}},true)}
document.addEventListener('pointermove',e=>{if(!mobileSelectDragging)return;if(mobileSelectPointerId!==null&&e.pointerId!==mobileSelectPointerId)return;e.preventDefault();const check=checkFromPoint(e);if(check)applyMobileMultiSelect(check.dataset.jobId,mobileSelectMode)},{passive:false});
document.addEventListener('pointerup',endMobileMultiSelect);document.addEventListener('pointercancel',endMobileMultiSelect);
async function selectJob(ev,id){const ids=jobs.map(j=>+j.id);if(ev&&(ev.shiftKey||ev.ctrlKey||ev.metaKey)){if(ev.shiftKey&&lastSelectedId!==null){const a=ids.indexOf(+lastSelectedId),b=ids.indexOf(+id);if(a>-1&&b>-1){const[from,to]=a<b?[a,b]:[b,a];for(let i=from;i<=to;i++)selectedJobs.add(ids[i])}}else if(ev.ctrlKey||ev.metaKey){if(selectedJobs.has(+id))selectedJobs.delete(+id);else selectedJobs.add(+id)}}lastSelectedId=+id;selectedId=id;toggleSide(false);renderJobs();await loadDetail(id)}
async function loadDetail(id){const d=await api(`job_detail&id=${id}`);if(!d.success){if(isAuthExpiredResponse(d)){handleAuthExpired(id);$('detailTitle').textContent=`Job #${id}`;$('detail').innerHTML=authExpiredBox(id);hideActionButtons();return}$('detail').innerHTML=`<div class="err">${esc(d.error)}</div>`;return}authExpired=false;detailCache=d;renderDetail(d.job,d.events||[])}
function hideActionButtons(){['downloadBtn','rerunSameBtn','rerunNewBtn','cancelBtn','deleteBtn'].forEach(id=>{if($(id))$(id).classList.add('hidden')})}
const LTX_TECH_QUALITY='smooth motion, stable footage, sharp details, high quality, natural motion blur, 180-degree shutter';
function finalPromptForJob(j){
  const s=j.settings||{};
  // PHOTO EDIT: do promptu nejdou žádné kamerové ani video-technické texty.
  if((s.input_mode||'')==='photo_edit'||isImageOutput(j)){
    return [j.prompt,s.style].map(x=>(x??'').toString().trim()).filter(Boolean).join(', ');
  }
  return [j.prompt,s.camera_motion,s.style,LTX_TECH_QUALITY].map(x=>(x??'').toString().trim()).filter(Boolean).join(', ');
}
function inputLanguageLabel(lang){
  const x=(lang||'').toString().trim().toLowerCase();
  if(x==='cs')return 'CZ';
  if(x==='en')return 'EN';
  return x?x.toUpperCase():'původní jazyk';
}
function originalPromptForJob(j){
  const s=j.settings||{};
  return ((s.original_prompt||j.prompt||'')+'').trim();
}
function originalNegativeForJob(j){
  const s=j.settings||{};
  return ((s.original_negative_prompt||j.negative_prompt||'')+'').trim();
}
async function copyFullPrompt(id){
  const d=detailCache&&detailCache.job&&+detailCache.job.id===+id?detailCache:{};
  const txt=d.job?finalPromptForJob(d.job):'';
  if(!txt)return;
  try{await navigator.clipboard.writeText(txt);alert('Kompletní prompt zkopírován.')}
  catch(e){prompt('Zkopíruj prompt:',txt)}
}
function selectOptionsHtml(selectId,selectedValue){return Array.from($(selectId)?.options||[]).map(opt=>`<option value="${esc(opt.value)}" ${String(opt.value)===String(selectedValue)?'selected':''}>${esc(opt.textContent||opt.value)}</option>`).join('')}
function detectFormatKey(w,h){w=+w||0;h=+h||0;if(w===1280&&h===720)return'hd_landscape';if(w===1920&&h===1080)return'fhd_landscape';if(w===720&&h===1280)return'hd_portrait';if(w===1080&&h===1920)return'fhd_portrait';if(w===1440&&h===1080)return'classic_4_3';if(w===1080&&h===1440)return'classic_3_4';if(w===1024&&h===1024)return'square';if(w===2000&&h===2000)return'square_2000';return'custom'}
function renderPendingEditor(j){const s=j.settings||{};const originalPrompt=(s.original_prompt||'').trim();const englishPrompt=(j.prompt||'').trim();const useOriginal=!!originalPrompt&&originalPrompt!==englishPrompt;const useCs=(s.input_language==='cs')||useOriginal;const promptVal=useOriginal?originalPrompt:(englishPrompt||originalPrompt);const originalNeg=(s.original_negative_prompt||'').trim();const englishNeg=(j.negative_prompt||'').trim();const negVal=(useOriginal&&originalNeg)?originalNeg:englishNeg;const langLabel=inputLanguageLabel(s.input_language||(useCs?'cs':'en'));const fmt=detectFormatKey(s.width,s.height);return `<div class="pending-editor"><div class="pending-editor-head"><b>${ui('Pending editace před renderem','Pending edit before rendering')}</b><span id="pendingEditState" class="pending-edit-state">${ui('Změny se ukládají automaticky.','Changes are saved automatically.')}</span></div><div class="pending-editor-body"><div class="field"><label>${ui('Prompt','Prompt')} (${esc(langLabel)} – ${ui('původní zadání','original input')})</label><textarea id="pendingPrompt" rows="4">${esc(promptVal)}</textarea></div><div class="field"><label>Negative (${esc(langLabel)})</label><textarea id="pendingNegative" rows="2">${esc(negVal)}</textarea></div><div class="grid3"><div class="field"><label>${ui('Rozlišení preset','Resolution preset')}</label><select id="pendingFormat">${selectOptionsHtml('format',fmt)}</select></div><div class="field"><label>Width</label><input id="pendingWidth" type="number" min="256" max="4096" step="1" value="${esc(s.width||1920)}"></div><div class="field"><label>Height</label><input id="pendingHeight" type="number" min="256" max="4096" step="1" value="${esc(s.height||1080)}"></div></div><div class="grid3"><div class="field"><label>FPS</label><input id="pendingFps" type="number" min="1" max="60" step="1" value="${esc(s.fps||25)}"></div><div class="field"><label>${ui('Délka (s)','Duration (s)')}</label><input id="pendingDuration" type="number" min="1" max="60" step="0.1" value="${esc(s.duration||5)}"></div><div class="field"><label>Seed</label><input id="pendingSeed" type="number" min="1" max="2147483647" step="1" value="${esc(s.seed||'')}"></div></div><div class="grid4"><div class="field"><label>${ui('Režim seedu','Seed mode')}</label><select id="pendingSeedMode"><option value="increment_batch" ${(s.seed_mode||'increment_batch')==='increment_batch'?'selected':''}>${ui('Batch: navyšovat seed','Batch: increment seed')}</option><option value="locked" ${(s.seed_mode||'')==='locked'?'selected':''}>${ui('Zamknout stejný seed','Lock same seed')}</option><option value="random_each" ${(s.seed_mode||'')==='random_each'?'selected':''}>${ui('Náhodný pro každý obrázek','Random for each image')}</option></select></div><div class="field"><label>Steps</label><input id="pendingSteps" type="number" min="1" max="200" step="1" value="${esc(s.steps||30)}"></div><div class="field"><label>CFG</label><input id="pendingCfg" type="number" min="0" max="30" step="0.1" value="${esc(s.cfg??3.5)}"></div><div class="field"><label>Motion strength</label><input id="pendingMotion" type="number" min="0" max="2" step="0.01" value="${esc(s.motion_strength??0.75)}"></div></div><div class="field"><label>${ui('Pohyb kamery — preset','Camera motion — preset')}</label><select id="pendingPreset">${selectOptionsHtml('preset',j.preset||'')}</select></div><div class="field"><label>Camera motion</label><textarea id="pendingCamera" rows="3">${esc(s.camera_motion||'')}</textarea></div><div class="field"><label>Style</label><textarea id="pendingStyle" rows="2">${esc(s.style||'')}</textarea></div><div class="grid2"><label class="checkline"><input id="pendingPromptEnhance" type="checkbox" ${s.prompt_enhance?'checked':''}> Prompt Enhance (LTX)</label><div class="field"><label>${ui('Délka vylepšeného promptu','Enhanced prompt length')}</label><input id="pendingEnhanceTokens" type="number" min="64" max="512" step="16" value="${esc(s.enhance_tokens||128)}"></div></div><div class="small">${ui('Upravovat jde jen stav','Only')} <b>pending</b>. ${ui('Jakmile worker job vezme, editace se zamkne.','When the worker starts the job, editing is locked.')}</div></div></div>`}
let pendingSaveTimer=null,pendingSaveBusy=false,pendingSaveQueued=false;function pendingEditState(msg,mode=''){const el=$('pendingEditState');if(!el)return;el.textContent=msg;el.className='pending-edit-state'+(mode?' '+mode:'')}
function collectPendingEditorPayload(id){const payload={id,prompt:($('pendingPrompt')?.value||'').trim(),negative_prompt:($('pendingNegative')?.value||'').trim(),preset:$('pendingPreset')?.value||'',settings:{width:+($('pendingWidth')?.value||0),height:+($('pendingHeight')?.value||0),fps:+($('pendingFps')?.value||0),duration:parseFloat(($('pendingDuration')?.value||'0').replace(',','.'))||0,seed:+($('pendingSeed')?.value||0),seed_mode:($('pendingSeedMode')?.value||'increment_batch'),steps:+($('pendingSteps')?.value||0),cfg:parseFloat(($('pendingCfg')?.value||'0').replace(',','.'))||0,motion_strength:parseFloat(($('pendingMotion')?.value||'0').replace(',','.'))||0,prompt_enhance:!!$('pendingPromptEnhance')?.checked,enhance_tokens:+($('pendingEnhanceTokens')?.value||128),camera_motion:($('pendingCamera')?.value||'').trim(),style:($('pendingStyle')?.value||'').trim()}};return payload}
async function savePendingEditor(id){const root=$('pendingPrompt');if(!root)return;if(pendingSaveBusy){pendingSaveQueued=true;return}const payload=collectPendingEditorPayload(id);if(!payload.prompt){pendingEditState('Prompt nesmí být prázdný.','error');return}pendingSaveBusy=true;pendingEditState('Ukládám…','saving');const d=await api('update_pending_job','POST',payload);pendingSaveBusy=false;if(!d.success){pendingEditState(d.error||'Uložení selhalo.','error');return}pendingEditState('Uloženo','ok');if(detailCache&&detailCache.job&&+detailCache.job.id===+id)detailCache.job=d.job;const idx=jobs.findIndex(x=>+x.id===+id);if(idx>-1)jobs[idx]=d.job;renderJobs();if(pendingSaveQueued){pendingSaveQueued=false;savePendingEditor(id)}}
async function choosePendingImage(id){const j=jobs.find(x=>+x.id===+id)||(detailCache&&detailCache.job);if(!j||String(j.status)!=='pending'){alert('Fotku lze změnit jen u pending jobu.');return}const inp=document.createElement('input');inp.type='file';inp.accept='image/*';inp.onchange=async()=>{const file=inp.files&&inp.files[0];if(!file)return;const fd=new FormData();fd.append('id',id);fd.append('image',file);pendingEditState('Nahrávám novou fotku…','saving');const d=await api('update_pending_image','POST',fd);if(!d.success){pendingEditState(d.error||'Změna fotky selhala.','error');alert(d.error||'Změna fotky selhala.');return}pendingEditState('Nová fotka uložena','ok');const idx=jobs.findIndex(x=>+x.id===+id);if(idx>-1)jobs[idx]=d.job;if(detailCache&&detailCache.job&&+detailCache.job.id===+id)detailCache.job=d.job;renderJobs();await loadDetail(id);};inp.click()}
function schedulePendingSave(id,immediate=false){clearTimeout(pendingSaveTimer);if(immediate)savePendingEditor(id);else{pendingEditState('Neuložené změny…');pendingSaveTimer=setTimeout(()=>savePendingEditor(id),700)}}
function wirePendingEditor(id){const root=$('pendingPrompt');if(!root)return;const bindIds=['pendingPrompt','pendingNegative','pendingWidth','pendingHeight','pendingFps','pendingDuration','pendingSeed','pendingSeedMode','pendingSteps','pendingCfg','pendingMotion','pendingPromptEnhance','pendingEnhanceTokens','pendingPreset','pendingCamera','pendingStyle'];bindIds.forEach(fid=>{const el=$(fid);if(!el)return;el.addEventListener('input',()=>schedulePendingSave(id,false));el.addEventListener('change',()=>schedulePendingSave(id,true));el.addEventListener('blur',()=>schedulePendingSave(id,true));});const fmt=$('pendingFormat');if(fmt){fmt.addEventListener('change',()=>{if(fmt.value==='auto_fhd'){const img=document.querySelector('#detail .preview img');if(img&&(img.naturalWidth||img.width)&&(img.naturalHeight||img.height)){const out=calcAutoFhdSize(img.naturalWidth||img.width,img.naturalHeight||img.height);$('pendingWidth').value=out.w;$('pendingHeight').value=out.h;}}else{const f=formatPresets[fmt.value];if(f){$('pendingWidth').value=f.w;$('pendingHeight').value=f.h}}schedulePendingSave(id,true)});}const whSync=()=>{const fmtKey=detectFormatKey($('pendingWidth')?.value,$('pendingHeight')?.value);if($('pendingFormat')&&$('pendingFormat').value!=='auto_fhd')$('pendingFormat').value=fmtKey};['pendingWidth','pendingHeight'].forEach(fid=>{const el=$(fid);if(el)el.addEventListener('input',whSync)});const presetSel=$('pendingPreset');if(presetSel){presetSel.addEventListener('change',()=>{const cam=$('pendingCamera');const old=(cam?.value||'').trim();if(!old||old===cameraPresetText(presetSel.dataset.prev||'')){cam.value=cameraPresetText(presetSel.value)||old;}presetSel.dataset.prev=presetSel.value;});presetSel.dataset.prev=presetSel.value||'';}}
function renderDetail(j,events){
  $('detailTitle').textContent=`Job #${j.id}`;
  hideActionButtons();
  if(j.output_url){$('downloadBtn').classList.remove('hidden');$('downloadBtn').onclick=()=>{location.href=j.output_url}}
  if(j.id){$('rerunSameBtn').classList.remove('hidden');$('rerunSameBtn').onclick=()=>requeueJobFromQueue(j.id,false);$('rerunNewBtn').classList.remove('hidden');$('rerunNewBtn').onclick=()=>requeueJobFromQueue(j.id,true)}
  if(['pending','processing','queued','generating','uploading','downloading'].includes(j.status)){$('cancelBtn').classList.remove('hidden');$('cancelBtn').onclick=()=>cancelJob(j.id)}
  $('deleteBtn').classList.remove('hidden');$('deleteBtn').onclick=()=>deleteJob(j.id);
  const s=j.settings||{};
  const fullPrompt=finalPromptForJob(j);
  const originalPrompt=originalPromptForJob(j);
  const originalNegative=originalNegativeForJob(j);
  const promptWasTranslated=!!(originalPrompt&&j.prompt&&originalPrompt!==String(j.prompt).trim());
  const negativeWasTranslated=!!(originalNegative&&j.negative_prompt&&originalNegative!==String(j.negative_prompt).trim());
  const langLabel=inputLanguageLabel(s.input_language||(promptWasTranslated?'cs':'en'));
  const promptBlock=`<p class="small"><b>${ui('Prompt','Prompt')} (${esc(langLabel)} – ${ui('původní zadání','original input')}):</b><br>${esc(originalPrompt||j.prompt||'')}</p>`+
    (promptWasTranslated?`<p class="small"><b>${ui('Prompt EN pro Comfy','Prompt EN for Comfy')}:</b><br>${esc(j.prompt)}</p>`:'');
  const negativeBlock=originalNegative?
    (`<p class="small"><b>Negative (${esc(langLabel)}):</b><br>${esc(originalNegative)}</p>`+(negativeWasTranslated?`<p class="small"><b>${ui('Negative EN pro Comfy','Negative EN for Comfy')}:</b><br>${esc(j.negative_prompt)}</p>`:'')):
    `<p class="small"><b>Negative:</b><br>workflow default</p>`;
  const outInline=j.output_url?(j.output_url+(j.output_url.includes('?')?'&':'?')+'inline=1'):'';
  let inputPreview='';
  if(j.input_url&&j.input2_url&&!j.output_url){inputPreview=`<div class="grid2"><div class="preview"><img src="${esc(j.input_url)}" alt="first frame"></div><div class="preview"><img src="${esc(j.input2_url)}" alt="last frame"></div></div>`}
  else if(j.input_url){inputPreview=`<div class="preview detail-preview-centered ${j.status==='pending'?'pending-photo-change':''}" ${j.status==='pending'?`onclick="choosePendingImage(${j.id})" title="Klepni pro změnu fotky"`:''}><img src="${esc(j.input_url)}" alt="input"></div>`}
  let video=j.output_url?(isImageOutput(j)?`<div class="video-wrap"><img src="${esc(outInline)}" alt="výsledek"></div>`:`<div class="video-wrap"><video controls playsinline preload="metadata" src="${esc(outInline)}"></video></div>`):inputPreview;
  const pct=smoothProgressValue(j);
  const id=+j.id;
  $('detail').innerHTML=`<div class="status-line"><span class="pill p-${esc(j.status)}">${esc(j.status)}</span><div class="bar"><span data-pz-progress="${id}" style="width:${pct}%"></span></div><b data-pz-progress-text="${id}">${pct}%</b></div>${video}${j.status==='pending'?renderPendingEditor(j):''}${j.error?`<div class="err">${esc(j.error)}</div>`:''}<div class="full-prompt"><div class="full-prompt-head"><span>${ui('Kompletní prompt do LTX / Comfy','Full prompt for LTX / Comfy')}</span><button class="mini-btn" type="button" onclick="copyFullPrompt(${j.id})">${ui('Kopírovat EN','Copy EN')}</button></div><pre>${esc(fullPrompt||j.prompt||'')}</pre></div>${promptBlock}${negativeBlock}<div class="kv"><div>Comfy prompt ID</div><div>${esc(j.comfy_prompt_id||'—')}</div><div>Current node</div><div>${esc(j.current_node||'—')}</div><div>${ui('Režim','Mode')}</div><div>${esc((s.input_mode||'1pict').toString().toLowerCase().includes('2')?'2 PICT / first-last frame':'1 PICT')}</div><div>Preset</div><div>${esc(j.preset||'—')}</div><div>${ui('Jazyk vstupu','Input language')}</div><div>${esc(s.input_language||'en')}</div><div>${ui('Překladač','Translator')}</div><div>${esc(s.translation_provider||'—')}</div><div>${ui('Nastavení','Settings')}</div><div>${esc(`${s.width||'?'}×${s.height||'?'} · ${s.fps||'?'} fps · ${s.duration||'?'} s · ${s.frame_count||'?'} frames · seed ${s.seed||'?'}`)}</div><div>${ui('Čas','Time')}</div><div>${esc((j.duration_seconds?Number(j.duration_seconds).toFixed(1)+' s':'—'))}</div><div>${ui('Vytvořeno','Created')}</div><div>${esc(j.created_at||'')}</div></div><div class="events">${events.map(e=>`<div class="event"><time>${esc(e.created_at)}</time> <b>${esc(e.type)}</b><br>${esc(e.message||'')}</div>`).join('')||'<div class="event">'+ui('Bez eventů.','No events.')+'</div>'}</div>`;if(j.status==='pending')wirePendingEditor(j.id)
}
function outputDownloadUrlForJob(j){if(!j||!j.id)return'';return j.output_url||`api.php?action=job_file&id=${encodeURIComponent(j.id)}&kind=output`}
function downloadableSelectedJobs(){return [...selectedJobs].map(id=>jobs.find(x=>+x.id===+id)).filter(j=>j&&String(j.status||'')==='done')}
async function downloadSelectedJobs(){if(!selectedJobs.size){alert('Nejdřív označ hotové joby ke stažení.');return}const ready=downloadableSelectedJobs();const skipped=selectedJobs.size-ready.length;if(!ready.length){alert('Mezi označenými není žádné hotové video ke stažení.');return}if(skipped>0&&!confirm(`Stáhnout ${ready.length} hotových videí? ${skipped} označených jobů ještě není hotových nebo nemá výstup.`))return;for(let i=0;i<ready.length;i++){const j=ready[i];const a=document.createElement('a');a.href=outputDownloadUrlForJob(j);a.download='';a.rel='noopener';a.style.display='none';document.body.appendChild(a);a.click();a.remove();if(i<ready.length-1)await waitMs(700)}const el=$('selInfo');if(el)el.textContent=ui('Stahuji: ','Downloading: ')+ready.length+ui(' souborů samostatně',' files separately')}
async function cancelSelectedJobs(){if(!selectedJobs.size){alert('Nejdřív označ joby.');return}const activeIds=[...selectedJobs].filter(id=>{const j=jobs.find(x=>+x.id===+id);return j&&canStopJobStatus(j.status)});if(!activeIds.length){alert('Mezi označenými není žádný aktivní render.');return}if(!confirm(`Zastavit ${activeIds.length} označených jobů?`))return;for(const id of activeIds){await api('cancel_job','POST',{id})}await loadJobs();if(selectedId&&activeIds.includes(+selectedId)&&jobs.find(x=>+x.id===+selectedId))await loadDetail(selectedId);else if(selectedId&&!jobs.find(x=>+x.id===+selectedId)){selectedId=null;$('detail').innerHTML='<div class="detail-empty">Označené rendery zastaveny.</div>';hideActionButtons()}alert(`Zastaveno jobů: ${activeIds.length}`)}
async function deleteSelectedJobs(){if(!selectedJobs.size){alert('Nejdřív označ joby.');return}if(!confirm(`Smazat ${selectedJobs.size} označených jobů?`))return;const ids=[...selectedJobs];for(const id of ids){await api('delete_job','POST',{id})}selectedJobs.clear();selectedId=null;await loadJobs();$('detail').innerHTML='<div class="detail-empty">'+ui('Označené joby smazány.','Selected jobs deleted.')+'</div>';hideActionButtons()}
document.addEventListener('keydown',e=>{if((e.key==='Delete'||e.key==='Backspace')&&selectedJobs.size&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();deleteSelectedJobs()}})
async function cancelJob(id){if(!confirm('Zrušit job?'))return;await api('cancel_job','POST',{id});await loadJobs();await loadDetail(id)}
async function deleteJob(id){if(!confirm('Smazat job i soubory?'))return;await api('delete_job','POST',{id});selectedId=null;await loadJobs();$('detail').innerHTML='<div class="detail-empty">'+ui('Job smazán.','Job deleted.')+'</div>';hideActionButtons()}
async function clearFinished(){if(!confirm('Smazat všechny hotové/chybové/zrušené joby?'))return;await api('clear_finished','POST',{});selectedId=null;await loadJobs();newJobForm()}

function diagStatusClass(status){status=String(status||'').toLowerCase();return status==='ok'?'diag-ok':(status==='warn'?'diag-warn':'diag-bad')}
async function openDiagnostics(){
  hideActionButtons();
  $('detailTitle').textContent=ui('Diagnostika','Diagnostics');
  $('detail').innerHTML='<div class="detail-empty">'+ui('Kontroluji systém…','Checking system…')+'</div>';
  const d=await api('diagnostics');
  if(!d.success){$('detail').innerHTML='<div class="err">'+esc(d.error||ui('Diagnostika selhala.','Diagnostics failed.'))+'</div>';return}
  const rows=(d.checks||[]).map(x=>`<div>${esc(x.name||'')}</div><div class="${diagStatusClass(x.status)}"><b>${esc((x.status||'').toUpperCase())}</b></div><div>${esc(x.message||'')}</div>`).join('');
  const workers=(d.workers||[]).map(w=>`<div class="event"><b>${esc(w.id||'worker')}</b><br>${ui('Verze','Version')}: ${esc(w.version||'old')} · ${ui('stav','state')}: ${esc(w.state||'')}</div>`).join('')||'<div class="event">'+ui('Žádný worker online.','No worker online.')+'</div>';
  $('detail').innerHTML=`<div class="diag-box"><h3>${ui('Rychlá kontrola','Quick check')}</h3><div class="diag-grid">${rows}</div></div><div class="diag-box"><h3>Workers</h3>${workers}</div><div class="small">${ui('Tahle diagnostika nic nemaže ani nemění. Jen kontroluje PHP, SQLite, složky, workflow, worker a Comfy stav z posledního signálu.','This diagnostic does not delete or change anything. It only checks PHP, SQLite, folders, workflows, worker and last Comfy signal.')}</div>`;
}

function renderStatsData(d){
  const workers=d.workers||{};const q=d.queue_counts||{};updateWorkerDots(workers);const now=Date.now();
  let html=`<span class="chip warn">${ui('Fronta','Queue')} <b>${q.active_total||0}</b></span><span class="chip">${ui('čeká','waiting')} <b>${q.pending||0}</b></span><span class="chip">${ui('render','rendering')} <b>${(q.processing||0)+(q.queued||0)+(q.generating||0)+(q.uploading||0)+(q.downloading||0)}</b></span><span class="chip">${ui('hotovo dnes','done today')} <b>${q.done_today||0}</b></span>`;
  let wh='';
  for(const[wid,x] of Object.entries(workers)){
    const g=x.gpu;const w=x.worker||{};const c=x.comfy||{};const ts=x.updated_at?new Date(x.updated_at).getTime():0;const age=ts?Math.abs(now-ts):999999999;
    const activeJob=Number(w.active_job||0);const online=ts>0&&age<240000;const waitSignal=!online&&activeJob>0&&age<1200000;const hardOffline=ts===0||age>=1200000;
    const label=wid.startsWith('DOMA-')?'🏠 DOMA':wid.startsWith('PRACE-')?'💼 PRÁCE':esc(wid);
    const cls=online?'':waitSignal?'warn':'bad';
    const state=online?'online':waitSignal?`${ui('čekám na signál','waiting for signal')} · job #${activeJob}`:'offline';
    wh+=`<span class="chip ${cls}">${label}: <b>${state}</b></span>`;
    if(online&&c&&c.online)wh+=`<span class="chip ok">ComfyUI <b>ready</b></span>`;
    else if(online&&c&&c.state==='starting')wh+=`<span class="chip info">ComfyUI <b>${ui('startuje','starting')}</b></span>`;
    else if(online&&c&&c.state==='start_timeout')wh+=`<span class="chip bad">ComfyUI <b>${ui('nenaběhlo','not started')}</b></span>`;
    else if(online&&c&&c.online===false)wh+=`<span class="chip bad" title="${esc(c.error||'Důvod zjistíš v Diagnostice.')}">ComfyUI <b>offline</b></span>`;
    if(online&&g){const util=(g.util_pct===null||g.util_pct===undefined)?'':`GPU <b>${g.util_pct}%</b> · `;wh+=`<span class="chip">${util}VRAM <b>${((g.mem_used_mb||0)/1024).toFixed(1)}/${((g.mem_total_mb||0)/1024).toFixed(1)} GB</b></span>`;}
    if(online&&g&&g.temp_c)wh+=`<span class="chip ${g.temp_c>82?'bad':''}">&#127777; <b>${g.temp_c}°C</b></span>`;
  }
  html+=wh||'<span class="chip bad">worker offline</span>';
  $('stats').innerHTML=html;
  renderStatsPeek(d);
}

// Zavřená záložka musí sama napovědět to podstatné: stav ComfyUI a frontu.
function renderStatsPeek(d){
  const peek=$('statsPeek');if(!peek)return;
  const q=d.queue_counts||{};
  let comfy=null;
  for(const x of Object.values(d.workers||{})){
    const c=x.comfy||{};
    if(c.online)comfy='ok';
    else if(c.online===false&&comfy===null)comfy='bad';
  }
  const bits=[comfy==='ok'?'<span class="chip ok">ComfyUI <b>ready</b></span>'
    :comfy==='bad'?'<span class="chip bad">ComfyUI <b>offline</b></span>'
    :'<span class="chip">ComfyUI <b>--</b></span>',
    `<span class="chip warn">${ui('Fronta','Queue')} <b>${q.active_total||0}</b></span>`];
  if(typeof d.jobs_ahead==='number'&&d.jobs_ahead>0)
    bits.push(`<span class="chip info">${ui('před tebou','ahead of you')} <b>${d.jobs_ahead}</b></span>`);
  if(d.eta_seconds)
    bits.push(`<span class="chip info" title="${ui('Odhad z průměru posledních renderů','Estimated from recent render times')}">`+
      `${ui('hotovo za','ready in')} <b>~${fmtEta(d.eta_seconds)}</b></span>`);
  peek.innerHTML=bits.join('');
}

// „hotovo za ~12 min" je čitelnější než 743 sekund.
function fmtEta(sec){
  const s=Math.max(0,Math.round(Number(sec)||0));
  if(s<90)return s+' s';
  const m=Math.round(s/60);
  if(m<60)return m+' min';
  const h=Math.floor(m/60);
  return h+' h '+(m%60)+' min';
}

// restartSelectedWorker/startSelectedComfy z původního webu tady nejsou:
// ComfyUI běží mimo aplikaci, ComfyLocal ho nespouští ani nerestartuje.
async function loadStats(){try{const d=await api('stats');if(d.success)renderStatsData(d)}catch(e){}}
const LIVE_STATUSES=['pending','processing','queued','generating','uploading','downloading'];
let pollTimer=null,pollInFlight=false;
function nextPollMs(){
  if(Date.now()<apiBackoffUntil)return 180000;
  if(document.hidden)return 180000;
  const live=jobs.some(j=>LIVE_STATUSES.includes(j.status));
  return live?8000:45000;
}
function schedulePoll(ms){clearTimeout(pollTimer);pollTimer=setTimeout(tick,ms||nextPollMs());}
async function refreshDashboard(force=false){
  if(bulkUploadActive && !force)return;
  if(pollInFlight)return;
  if(!force && !isPollLeader())return;
  pollInFlight=true;
  try{
    const params=[];
    if(selectedId)params.push('detail_id='+encodeURIComponent(selectedId));
    if(force)params.push('force=1');
    params.push('limit=250');const action='dashboard_cached'+(params.length?'&'+params.join('&'):'');
    const d=await api(action);
    if(!d.success){if(isAuthExpiredResponse(d)&&selectedId){$('detail').innerHTML=authExpiredBox(selectedId)}return;}
    applyDashboardPayload(d);
    broadcastDashboard(d);
  }catch(e){}finally{pollInFlight=false;}
}
async function tick(){await refreshDashboard();schedulePoll();}
document.addEventListener('visibilitychange',()=>{if(!document.hidden){clearTimeout(pollTimer);tick();}});
function bootFastRefresh(){const box=$('jobs');if(box&&!jobs.length)box.innerHTML='<div class="small" style="padding:12px">'+ui('Načítám frontu…','Loading queue…')+'</div>';refreshDashboard(true);const bursts=JUST_LOGGED_IN?[180,650,1400,2800,5200]:[350,1200];bursts.forEach(ms=>setTimeout(()=>refreshDashboard(true),ms));schedulePoll(JUST_LOGGED_IN?6000:8000)}
setAppLang(getInitialAppLang());
updateNotifyButton();
bindPromptClearButton();
// PZ FIX: výchozí jazyk PROMPTU je vlaječka CZ, ne jazyk celé aplikace.
// To znamená: uživatel píše česky a před odesláním se prompt překládá CZ → EN.
// PZ FIX: při načtení stránky zůstane výchozí jazyk promptu Čeština.
// Zároveň nesmíme volat syncFormatFromSize(),
// protože defaultní width/height 1920×1080 by okamžitě přepsaly
// vybraný režim "Auto podle fotky · FHD limit" na pevný FHD horizontal preset.
if($('format')) $('format').value='auto_fhd';
// PZ FIX: výchozí pohyb kamery je Statická kamera (stativ).
// Musí být nastavený jak select, tak textové pole Camera motion, aby se do jobu
// neposlal starý default Decentní nájezd dopředu.
if($('preset')) $('preset').value='Statická kamera (stativ)';
if($('camera')) { $('camera').value=cameraPresetText('Statická kamera (stativ)'); $('camera').dataset.pzDirty='0'; }
updateMobSummary();
translateStaticText();
updateSelectionInfo();
bootFastRefresh();

/* === Mobil: zavření boční fronty tažením prstu (swipe-to-close) === */
(function(){
  var side=document.getElementById('side'), mask=document.getElementById('mask');
  if(!side) return;
  var startX=0,startY=0,dx=0,w=0,t0=0,dragging=false,decided=false,horiz=false;
  function isMobile(){return window.matchMedia('(max-width:900px)').matches;}
  side.addEventListener('touchstart',function(e){
    if(!isMobile()||!side.classList.contains('open')||e.touches.length!==1)return;
    startX=e.touches[0].clientX;startY=e.touches[0].clientY;
    dx=0;w=side.offsetWidth||244;t0=Date.now();dragging=true;decided=false;horiz=false;
  },{passive:true});
  side.addEventListener('touchmove',function(e){
    if(!dragging)return;
    var ddx=e.touches[0].clientX-startX, ddy=e.touches[0].clientY-startY;
    if(!decided){
      if(Math.abs(ddx)<6&&Math.abs(ddy)<6)return;
      decided=true;horiz=Math.abs(ddx)>Math.abs(ddy);
    }
    if(!horiz)return;               /* svislý pohyb = scroll fronty */
    dx=Math.min(0,ddx);             /* jen tažení doleva = zavírání */
    side.style.transition='none';
    side.style.transform='translateX('+dx+'px)';
    if(mask)mask.style.opacity=String(Math.max(0,1+dx/w));
    e.preventDefault();
  },{passive:false});
  function settle(){
    if(!dragging)return;
    dragging=false;
    side.style.transition='';side.style.transform='';
    if(mask)mask.style.opacity='';
    if(!horiz)return;
    var dist=Math.abs(dx),vel=dist/Math.max(1,Date.now()-t0);
    if(dist>w*0.33||vel>0.5){ if(typeof toggleSide==='function')toggleSide(false); }
  }
  side.addEventListener('touchend',settle);
  side.addEventListener('touchcancel',settle);
})();


/* ── PIN (nahrazuje login uživatelů z webu) ──────────────── */
function showPinGate(){const gate=$('pinGate'),wrap=$('appWrap');if(gate)gate.style.display='grid';if(wrap)wrap.style.display='none';const i=$('pinInput');if(i)i.focus()}
function hidePinGate(){const gate=$('pinGate'),wrap=$('appWrap');if(gate)gate.style.display='none';if(wrap)wrap.style.display=''}
(function(){
  const form=$('pinForm');if(!form)return;
  form.addEventListener('submit',async e=>{
    e.preventDefault();
    const err=$('pinError');const fd=new FormData();fd.append('pin',$('pinInput').value||'');
    // Při zapnutých účtech server chce i jméno — bez tohohle se přihlásit nedá.
    const u=$('userInput');if(u)fd.append('username',u.value||'');
    const d=await api('login','POST',fd);
    if(d&&d.success){if(err)err.style.display='none';hidePinGate();location.reload();return}
    if(err){err.textContent=(d&&d.error)||'PIN nesedí.';err.style.display=''}
  });
})();
