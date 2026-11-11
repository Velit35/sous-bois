"use strict";
/* Sous-Bois — carte des zones à champignons et carnet de cueillette.
   Données préparées sur GitHub (dossier data/) + météo en direct d'Open-Meteo en dehors de la zone. */

/* ====================================================================== Outils */
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=(n,d=0)=>n==null||isNaN(n)?"–":Number(n).toLocaleString("fr-FR",{minimumFractionDigits:d,maximumFractionDigits:d});
const clamp=(x,a=0,b=1)=>Math.max(a,Math.min(b,x));
const avg=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:null;
const isoOf=d=>new Date(d.getTime()-d.getTimezoneOffset()*6e4).toISOString().slice(0,10);
const todayISO=()=>isoOf(new Date());
const addDays=(iso,n)=>{const d=new Date(iso+"T12:00:00");d.setDate(d.getDate()+n);return isoOf(d)};
const dayDiff=(a,b)=>Math.round((new Date(b+"T12:00:00")-new Date(a+"T12:00:00"))/864e5);
const round1=v=>v==null?null:Math.round(v*10)/10;
const uid=()=>Date.now().toString(36)+Math.random().toString(36).slice(2,6);
const dateFR=(iso,o={day:"numeric",month:"short",year:"numeric"})=>new Date(iso+"T12:00").toLocaleDateString("fr-FR",o);
let toastTimer;
function toast(msg,ms=2800){const t=$("#toast");t.textContent=msg;t.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.hidden=true,ms)}
async function fetchJSON(url,ms=15000){const c=new AbortController();const t=setTimeout(()=>c.abort(),ms);
  try{const r=await fetch(url,{signal:c.signal});if(!r.ok)throw new Error("HTTP "+r.status);return await r.json()}finally{clearTimeout(t)}}
function haversine(a,b){const R=6371e3,r=Math.PI/180,dl=(b[0]-a[0])*r,dg=(b[1]-a[1])*r;
  const h=Math.sin(dl/2)**2+Math.cos(a[0]*r)*Math.cos(b[0]*r)*Math.sin(dg/2)**2;return 2*R*Math.asin(Math.sqrt(h))}
const store={get(k,d){try{const v=localStorage.getItem(k);return v==null?d:JSON.parse(v)}catch(e){return d}},
  set(k,v){try{localStorage.setItem(k,JSON.stringify(v));return true}catch(e){return false}},
  del(k){try{localStorage.removeItem(k)}catch(e){}}};
try{navigator.storage&&navigator.storage.persist&&navigator.storage.persist()}catch(e){}

/* ====================================================================== Espèces */
// rain : pluie efficace (mm, 15 derniers jours + moitié des 15 d'avant) ; temp : moyenne de l'air sur 7 j en trapèze ; frost : gel qui bloque la pousse
const SPECIES={
  cepe:{name:"Cèpe de Bordeaux",latin:"Boletus edulis",color:"#8A4B1F",hosts:"chêne, hêtre, châtaignier, pins, épicéa",soil:"acide à neutre (pH 4,5–6,5)",season:"août → novembre",
    rain:{min:20,opt:55},temp:{lo:5,opt1:9,opt2:17,hi:23},frost:-1},
  girolle:{name:"Girolle",latin:"Cantharellus cibarius",color:"#E3A21A",hosts:"chêne, hêtre, châtaignier, pins",soil:"acide, souvent moussu",season:"juin → novembre",
    rain:{min:15,opt:45},temp:{lo:6,opt1:10,opt2:20,hi:26},frost:-1},
  craterelle:{name:"Chanterelle en tube",latin:"Craterellus tubaeformis",color:"#9C7A2E",hosts:"conifères et feuillus, dans la mousse",soil:"acide, humide",season:"octobre → janvier",
    rain:{min:25,opt:60},temp:{lo:1,opt1:5,opt2:13,hi:18},frost:-4},
  trompette:{name:"Trompette de la mort",latin:"Craterellus cornucopioides",color:"#3B3B46",hosts:"hêtre, chêne, charme",soil:"argileux, plutôt neutre",season:"septembre → décembre",
    rain:{min:25,opt:60},temp:{lo:3,opt1:7,opt2:15,hi:20},frost:-3},
  pdm:{name:"Pied-de-mouton",latin:"Hydnum repandum",color:"#D9B98B",hosts:"feuillus et conifères",soil:"tous types, aime l'humus",season:"septembre → décembre",
    rain:{min:20,opt:50},temp:{lo:2,opt1:6,opt2:15,hi:20},frost:-3},
};
const EXTRA={cepe_pins:{name:"Cèpe des pins",color:"#6E2E1E"},bolet_bai:{name:"Bolet bai",color:"#7A5A3A"},
  coulemelle:{name:"Coulemelle",color:"#B9A27E"},rose_pres:{name:"Rosé des prés",color:"#D98C8C"},autre:{name:"Autre",color:"#6B7A6E"}};
const ALL={...SPECIES,...EXTRA};
const TREES=["Chêne","Hêtre","Châtaignier","Charme","Bouleau","Pin","Épicéa","Douglas","Mélèze","Mixte"];
const QTY=[["1","1"],["2-5","2 à 5"],["6-15","6 à 15"],["16+","16 et plus"]];
const RATINGS=["Rien","Faible","Modérée","Forte","Exceptionnelle"];
// Affinité de chaque espèce avec les peuplements (0 à 1), dans l'ordre des classes du fichier data/meta.json :
// 0 hors forêt, 1 chênes, 2 hêtre, 3 châtaignier, 4 autres feuillus, 5 mélange feuillus, 6 pins, 7 douglas, 8 épicéa/sapin,
// 9 autres conifères, 10 mixte, 11 peupleraie, 12 forêt ouverte, 13 lande, 14 haie / talus boisé, 15 petit bois
const AFF={
  cepe:      [0,1.0,0.9,0.9,0.55,0.7,0.7,0.4,0.6,0.4,0.8,0.05,0.4,0.1,0.45,0.6],
  girolle:   [0,0.9,0.9,1.0,0.55,0.7,0.8,0.4,0.5,0.4,0.8,0.05,0.4,0.1,0.45,0.6],
  craterelle:[0,0.5,0.6,0.5,0.3,0.5,0.8,0.7,1.0,0.7,0.8,0.05,0.3,0.1,0.1,0.35],
  trompette: [0,0.8,1.0,0.6,0.55,0.8,0.2,0.2,0.2,0.2,0.5,0.1,0.3,0,0.15,0.45],
  pdm:       [0,0.8,0.9,0.7,0.5,0.8,0.6,0.6,0.7,0.6,0.9,0.1,0.4,0.05,0.25,0.5],
};
const PHPREF={cepe:[4.5,6.5],girolle:[4,6],craterelle:[4,6],trompette:[5.5,7.5],pdm:[4.5,7.5]};

/* ====================================================================== État */
let finds=store.get("sb.finds",[]);
let outings=store.get("sb.outings",[]);
let active=store.get("sb.active",null);           // sortie en cours
const DEFAULT_LAYERS={zones:true,paths:true,meteo:false,forest:false,geol:false,ph:false,finds:true,cadastre:false,pubOutline:false};
const prefs=Object.assign({species:"cepe",day:0,base:"sat",own:"all",collapsed:false,layers:{...DEFAULT_LAYERS},view:{lat:48.15,lon:-1.62,z:11}},store.get("sb.prefs",{}));
if(prefs.v!==3){prefs.v=3;prefs.base="sat";prefs.own=prefs.own||"all";prefs.layers={...DEFAULT_LAYERS}}
for(const k in DEFAULT_LAYERS)if(!(k in prefs.layers))prefs.layers[k]=DEFAULT_LAYERS[k];
const savePrefs=()=>store.set("sb.prefs",prefs);
let calVersion=1;                                   // change dès que les données perso ou le modèle changent
function saveFinds(){if(!store.set("sb.finds",finds))toast("Mémoire pleine : exporte tes données.");calVersion++;updateCount()}
function saveOutings(){if(!store.set("sb.outings",outings))toast("Mémoire pleine : exporte tes données.");calVersion++}
function saveActive(){active?store.set("sb.active",active):store.del("sb.active")}

/* ====================================================================== Météo */
const PAST=30, FC=4;
function parseWx(j,smKey,tsKey){
  const d=j.daily,h=j.hourly||{};const smH=h[smKey]||[],tsH=h[tsKey]||[];
  const dm=(arr,i)=>{const v=arr.slice(i*24,i*24+24).filter(x=>x!=null);return v.length?avg(v):null};
  return {elev:j.elevation,days:d.time.map((t,i)=>{
    const tmin=d.temperature_2m_min[i],tmax=d.temperature_2m_max[i];
    let tmean=d.temperature_2m_mean?d.temperature_2m_mean[i]:null;if(tmean==null&&tmin!=null&&tmax!=null)tmean=(tmin+tmax)/2;
    return {d:t,p:d.precipitation_sum[i]??0,tmin,tmax,tmean,sm:dm(smH,i),ts:dm(tsH,i)}})};
}
async function forecastWx(points,past=PAST,fc=FC){
  const lat=points.map(p=>p.lat.toFixed(4)).join(","),lon=points.map(p=>p.lon.toFixed(4)).join(",");
  const url=`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&daily=precipitation_sum,temperature_2m_min,temperature_2m_max,temperature_2m_mean&hourly=soil_moisture_3_to_9cm,soil_temperature_6cm&past_days=${past}&forecast_days=${fc}&timezone=Europe%2FParis`;
  let j=await fetchJSON(url,30000);if(!Array.isArray(j))j=[j];
  return j.map(x=>parseWx(x,"soil_moisture_3_to_9cm","soil_temperature_6cm"));
}
async function archiveWx(p,start,end){
  const url=`https://archive-api.open-meteo.com/v1/archive?latitude=${p.lat.toFixed(4)}&longitude=${p.lon.toFixed(4)}&start_date=${start}&end_date=${end}&daily=precipitation_sum,temperature_2m_min,temperature_2m_max,temperature_2m_mean&hourly=soil_moisture_0_to_7cm,soil_temperature_0_to_7cm&timezone=Europe%2FParis`;
  return parseWx(await fetchJSON(url,30000),"soil_moisture_0_to_7cm","soil_temperature_0_to_7cm");
}
// Mêmes indicateurs que scripts/common.py
function metrics(days,t){
  const v=(i,k)=>days[i]?days[i][k]:null;
  let r30=0,r15=0,r7=0;
  for(let i=t-30;i<t;i++){const p=v(i,"p")||0;r30+=p;if(i>=t-15)r15+=p;if(i>=t-7)r7+=p}
  const rng=(a,b,k)=>{const o=[];for(let i=a;i<=b;i++){const x=v(i,k);if(x!=null)o.push(x)}return o};
  const tm=rng(t-6,t,"tmean"),fm=rng(t-5,t,"tmin"),tsN=rng(t-2,t,"ts"),tsA=rng(t-9,t-5,"ts"),tsB=rng(t-3,t,"ts");
  const a=avg(tsA),b=avg(tsB);
  return {r30,r15,r7,er:r15+0.5*(r30-r15),t7:avg(tm),fmin:fm.length?Math.min(...fm):null,sm:v(t,"sm"),ts:avg(tsN),drop:a!=null&&b!=null?a-b:null};
}

/* ====================================================================== Modèle */
let MODEL=null;
const quant=(a,q)=>{const s=[...a].sort((x,y)=>x-y);const i=(s.length-1)*q;const lo=Math.floor(i),hi=Math.ceil(i);return s[lo]+(s[hi]-s[lo])*(i-lo)};
// Données perso qui servent au calibrage : trouvailles et sorties réussies (les fortes comptent double)
function personalPresences(key){
  const out=[];
  for(const f of finds)if(f.species===key&&f.wx&&f.wx.m&&f.wx.m.t7!=null){out.push(f.wx.m);if(f.qty==="16+")out.push(f.wx.m)}
  for(const o of outings){const r=o.ratings&&o.ratings[key];if(r>=1&&o.wx&&o.wx.m&&o.wx.m.t7!=null){out.push(o.wx.m);if(r>=3)out.push(o.wx.m)}}
  return out;
}
const profCache=new Map();
function profileFor(key){
  const base=SPECIES[key];if(!base)return null;
  const ck=key+"|"+calVersion;if(profCache.has(ck))return profCache.get(ck);
  const mine=personalPresences(key);let P;
  if(mine.length<3)P={...base,calibrated:0};
  else{
    const r=mine.map(m=>m.er??m.r15),t=mine.map(m=>m.t7);
    const mix=(a,b)=>Math.round((a+b)/2*10)/10;
    P={...base,calibrated:mine.length,
      rain:{min:mix(base.rain.min,Math.max(5,Math.min(...r)*0.7)),opt:mix(base.rain.opt,quant(r,0.5))},
      temp:{lo:mix(base.temp.lo,Math.min(...t)-2),opt1:mix(base.temp.opt1,quant(t,0.25)),opt2:mix(base.temp.opt2,quant(t,0.75)),hi:mix(base.temp.hi,Math.max(...t)+2)}};
  }
  profCache.clear();profCache.set(ck,P);return P;
}
function parts(m,P){
  if(!P||!m||m.t7==null)return null;
  const rain=clamp(((m.er??m.r15)-P.rain.min)/Math.max(1,P.rain.opt-P.rain.min));
  const T=P.temp,t=m.t7;
  const temp=t<=T.lo||t>=T.hi?0:t<T.opt1?(t-T.lo)/(T.opt1-T.lo):t>T.opt2?(T.hi-t)/(T.hi-T.opt2):1;
  const soil=m.sm!=null?clamp((m.sm-0.18)/0.15):null;
  // Humidité : la pluie récente, ou un sol resté humide (fréquent en automne, quand l'eau s'évapore peu)
  const wet=soil==null?rain:Math.max(rain,0.7*soil+0.3*rain);
  const frost=m.fmin!=null&&m.fmin<P.frost;
  const shock=m.drop!=null?clamp((m.drop-0.5)/3):0;     // baisse de la température du sol : déclencheur classique
  return {rain,soil,wet,temp,fm:frost?0.35:1,shock,frost};
}
function combine(wet,temp,fm,shock){
  let s=0.65*wet+0.35*temp;
  if(temp<0.15)s*=0.4; if(wet<0.15)s*=0.4;
  s*=fm; if(wet>=0.4)s*=1+0.15*shock;
  return clamp(s);
}
function weatherModel(key){const M=MODEL&&MODEL.species&&MODEL.species[key]&&MODEL.species[key].weather;return M&&M.auc>=0.6&&M.n>=30?M:null}
function modelFactor(key,m){
  const M=weatherModel(key);if(!M)return 1;
  const x=[m.er,m.r7,m.t7,m.t7!=null?m.t7*m.t7:null,m.ts,m.drop,m.fmin];
  if(x.some(v=>v==null||isNaN(v)))return 1;
  let z=M.intercept;for(let i=0;i<x.length;i++)z+=M.coef[i]*(x[i]-M.mean[i])/M.std[i];
  return 0.6+0.8/(1+Math.exp(-z));
}
function score(m,key){const P=profileFor(key);const p=parts(m,P);if(!p)return null;const mf=modelFactor(key,m);return {...p,mf,s:clamp(combine(p.wet,p.temp,p.fm,p.shock)*mf)}}
function label(s){return s==null?"–":s<0.25?"Faible":s<0.5?"Moyen":s<0.75?"Bon":"Très bon"}
function ramp(s){const st=[[0.25,[244,211,94]],[0.6,[238,150,75]],[1,[209,73,91]]];
  if(s<=st[0][0])return `rgb(${st[0][1]})`;
  for(let i=1;i<st.length;i++){if(s<=st[i][0]){const[a,ca]=st[i-1],[b,cb]=st[i];const k=(s-a)/(b-a);return `rgb(${ca.map((v,j)=>Math.round(v+(cb[j]-v)*k))})`}}
  return `rgb(${st[2][1]})`}

/* ====================================================================== Grilles météo */
// static : calcul de la nuit sur GitHub (≈ 4 km) ; live : Open-Meteo en direct (≈ 7 km), en dehors de la zone ou en secours
const GRIDS={static:null,live:{kind:"live",dlon:0.1,dlat:0.067,nodes:new Map(),stamp:1}};
(function restoreLive(){const c=store.get("sb.wx3",null);if(!c||c.day!==todayISO()||Date.now()-c.t>3*3600e3)return;
  for(const[k,v]of Object.entries(c.data))GRIDS.live.nodes.set(k,v);GRIDS.live.stamp=c.t})();
function persistLive(){const data={};for(const[k,v]of GRIDS.live.nodes)data[k]=v;if(!store.set("sb.wx3",{day:todayISO(),t:GRIDS.live.stamp,data}))store.del("sb.wx3")}
async function loadMeteo(){
  try{
    const r=await fetch("data/meteo.json",{cache:"no-cache"});if(!r.ok)return;
    const j=await r.json();const off=j.days.indexOf(todayISO());
    if(off<0){STATUS.meteo="ancienne";return}
    const F=j.fields,nf=F.length,nodes=new Map();let w=999,e=-999,s=999,n=-999;
    for(const row of j.nodes){
      const mets=[];
      for(let d=off;d<j.days.length;d++){const m={};F.forEach((f,k)=>m[f]=row[3+d*nf+k]);mets.push(m)}
      nodes.set(row[0]+","+row[1],{elev:row[2],mets});
      const lon=row[0]*j.grid.dlon,lat=row[1]*j.grid.dlat;w=Math.min(w,lon);e=Math.max(e,lon);s=Math.min(s,lat);n=Math.max(n,lat);
    }
    GRIDS.static={kind:"static",dlon:j.grid.dlon,dlat:j.grid.dlat,nodes,stamp:Date.now(),generated:j.generated,ndays:j.days.length-off,ext:{w,e,s,n}};
    STATUS.meteo="ok";refreshAll();
  }catch(e){}
}
function staticCovers(lat,lon){const G=GRIDS.static;if(!G||G.ndays<=prefs.day)return false;const x=G.ext;return lat>=x.s&&lat<=x.n&&lon>=x.w&&lon<=x.e}
function gridFor(lat,lon){return staticCovers(lat,lon)?GRIDS.static:GRIDS.live}
function nodeKeysFor(G,b){const out=[];for(let i=Math.floor(b.w/G.dlon);i<=Math.ceil(b.e/G.dlon);i++)for(let j=Math.floor(b.s/G.dlat);j<=Math.ceil(b.n/G.dlat);j++)out.push({i,j,key:i+","+j,lat:j*G.dlat,lon:i*G.dlon});return out}
let busy=false;
async function ensureWeather(force){
  if(busy)return;
  const vb=map.getBounds();const b={w:vb.getWest(),e:vb.getEast(),s:vb.getSouth(),n:vb.getNorth()};
  const covered=staticCovers(b.s,b.w)&&staticCovers(b.n,b.e);
  if(covered&&!force)return;
  const G=GRIDS.live;if(force)G.nodes.clear();
  const nodes=nodeKeysFor(G,b);
  if(nodes.length>700){updateTop();return}
  const missing=nodes.filter(p=>!G.nodes.has(p.key)&&!staticCovers(p.lat,p.lon));
  if(!missing.length)return;
  busy=true;$("#btnRefresh").classList.add("busy");
  try{
    for(let k=0;k<missing.length;k+=100){const chunk=missing.slice(k,k+100);const res=await forecastWx(chunk);
      chunk.forEach((p,j)=>{const wx=res[j];const mets=[];for(let d=0;d<FC;d++)mets.push(metrics(wx.days,PAST+d));G.nodes.set(p.key,{elev:wx.elev,mets})})}
    G.stamp=Date.now();persistLive();refreshAll();
  }catch(e){toast("Météo indisponible pour l'instant. Réessaie dans un moment.")}
  finally{busy=false;$("#btnRefresh").classList.remove("busy")}
}
// Composantes du score par nœud, mémorisées pour l'espèce et le jour affichés
const partsCache=new Map();
function nodeParts(G){
  const k=G.kind+"|"+G.stamp+"|"+prefs.species+"|"+prefs.day+"|"+calVersion;
  let m=partsCache.get(k);if(m)return m;
  m=new Map();
  if(SPECIES[prefs.species])for(const[key,nd]of G.nodes){const met=nd.mets[prefs.day];const r=met&&score(met,prefs.species);if(r)m.set(key,r)}
  for(const kk of partsCache.keys())if(kk.startsWith(G.kind+"|"))partsCache.delete(kk);
  partsCache.set(k,m);return m;
}
const PK=["wet","temp","fm","shock","mf"];
function interp(G,lat,lon){
  const S=nodeParts(G);const x=lon/G.dlon,y=lat/G.dlat,i=Math.floor(x),j=Math.floor(y),fx=x-i,fy=y-j;
  const acc={wet:0,temp:0,fm:0,shock:0,mf:0};let ws=0;
  const add=(ii,jj,w)=>{const v=S.get(ii+","+jj);if(v&&w>0){for(const k of PK)acc[k]+=v[k]*w;ws+=w}};
  add(i,j,(1-fx)*(1-fy));add(i+1,j,fx*(1-fy));add(i,j+1,(1-fx)*fy);add(i+1,j+1,fx*fy);
  if(ws<0.2)return null;for(const k of PK)acc[k]/=ws;return acc;
}
function nearestNode(lat,lon){const G=gridFor(lat,lon);return G.nodes.get(Math.round(lon/G.dlon)+","+Math.round(lat/G.dlat))||null}

/* ====================================================================== Terrain préparé */
const TERRAIN={ready:false,meta:null,cls:null,rel:null,loss:null,pub:null,ph:null,error:null};
const STATUS={meteo:null};
async function loadBin(url){
  const r=await fetch(url,{cache:"no-cache"});if(!r.ok)throw new Error(url+" : "+r.status);
  const buf=new Uint8Array(await r.arrayBuffer());
  if(buf[0]===0x1f&&buf[1]===0x8b){
    if(!("DecompressionStream" in window))throw new Error("Navigateur trop ancien pour lire les zones (mets iOS à jour).");
    return new Uint8Array(await new Response(new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer());
  }
  return buf;
}
async function loadTerrain(){
  try{
    const r=await fetch("data/meta.json",{cache:"no-cache"});
    if(!r.ok){TERRAIN.error="pending";updateZoneStatus();return}
    const meta=await r.json();
    const g=meta.grid||meta.terrain;meta.grid=g;
    const files=meta.files||{terrain:meta.terrain.file};
    const size=g.w*g.h;
    const opt=async k=>{if(!files[k])return null;try{const a=await loadBin("data/"+files[k]);return a.length===size?a:null}catch(e){return null}};
    const cls=await loadBin("data/"+files.terrain);
    if(cls.length!==size)throw new Error("Fichier de zones incomplet");
    const [rel,loss,pub]=await Promise.all([opt("relief"),opt("loss"),opt("pub")]);
    let ph=null;if(meta.ph){try{ph=await loadBin("data/"+meta.ph.file)}catch(e){}}
    Object.assign(TERRAIN,{ready:true,meta,cls,rel,loss,pub,ph,error:null});
    calVersion++;updateZoneStatus();refreshAll();
  }catch(e){TERRAIN.error=e.message||"erreur";updateZoneStatus()}
}
function idxAt(g,lat,lon){const c=Math.floor((lon-g.west)/g.dlon),r=Math.floor((g.north-lat)/g.dlat);return c<0||r<0||c>=g.w||r>=g.h?-1:r*g.w+c}
function terrainAt(lat,lon){
  if(!TERRAIN.ready)return null;const i=idxAt(TERRAIN.meta.grid,lat,lon);if(i<0)return null;
  return {i,cls:TERRAIN.cls[i],rel:TERRAIN.rel?TERRAIN.rel[i]:0,loss:TERRAIN.loss?TERRAIN.loss[i]:0,pub:TERRAIN.pub?TERRAIN.pub[i]:null,ph:phLocal(lat,lon)};
}
function phLocal(lat,lon){if(!TERRAIN.ph)return null;const i=idxAt(TERRAIN.meta.ph,lat,lon);if(i<0)return null;const v=TERRAIN.ph[i];return v?v/10:null}
function className(c){return TERRAIN.meta&&TERRAIN.meta.classes[c]||"?"}
function phFactor(ph,key){const r=PHPREF[key];if(ph==null||!r)return 1;const d=ph<r[0]?r[0]-ph:ph>r[1]?ph-r[1]:0;return Math.max(0.5,1-0.5*d)}
function lossFactor(v){if(!v)return 1;const yrs=new Date().getFullYear()-(2000+v);return yrs<=4?0.25:yrs<=10?0.6:0.85}
function reliefWet(wet,rv){return rv?clamp(wet+(rv/255-0.5)*0.5):wet}

// Affinités effectives : réglage de base, corrigé par les observations publiques puis par tes sorties
function trackCells(o){
  const g=TERRAIN.meta.grid,set=new Set(),t=o.track||[];
  for(let k=0;k<t.length;k++){
    const a=t[k],b=t[k+1]||a;const n=Math.max(1,Math.ceil(haversine(a,b)/25));
    for(let s=0;s<n;s++){const la=a[0]+(b[0]-a[0])*s/n,lo=a[1]+(b[1]-a[1])*s/n;const i=idxAt(g,la,lo);if(i>=0)set.add(i)}
  }
  return set;
}
function personalHabitat(key){
  if(!TERRAIN.ready)return null;
  const outs=outings.filter(o=>(o.species||[]).includes(key)&&o.track&&o.track.length>3);
  if(outs.length<3)return null;
  const E=new Array(16).fill(0),F=new Array(16).fill(0);
  for(const o of outs){for(const i of trackCells(o)){const c=TERRAIN.cls[i];if(c)E[c]++}}
  for(const f of finds){if(f.species!==key)continue;const t=terrainAt(f.lat,f.lon);if(t&&t.cls)F[t.cls]++}
  const Ft=F.reduce((a,b)=>a+b,0),Et=E.reduce((a,b)=>a+b,0);
  if(Ft<3||Et<100)return null;
  const R=Ft/Et;
  return {mult:E.map((e,k)=>clamp((F[k]+2)/(e*R+2),0.6,1.5)),outs:outs.length,F:Ft};
}
const affCache=new Map();
function affEff(key){
  const ck=key+"|"+calVersion;if(affCache.has(ck))return affCache.get(ck);
  const base=AFF[key];if(!base)return null;
  const H=MODEL&&MODEL.species&&MODEL.species[key]&&MODEL.species[key].habitat;
  const w=H&&H.N>=30&&H.aff?H.N/(H.N+100):0;   // trop peu d'observations précises dans la zone : on garde le réglage de base
  const pers=personalHabitat(key);
  const out=base.map((a,k)=>{if(!a)return 0;let v=w?(1-w)*a+w*(H.aff[k]??a):a;if(pers)v*=pers.mult[k];return clamp(v,0,1.2)});
  out.learnedN=w?H.N:0;out.personal=pers;
  affCache.clear();affCache.set(ck,out);return out;
}

/* ====================================================================== Niveaux */
const LEVELS=[null,{name:"Possible",min:0.22,rgba:[244,211,94,170]},{name:"Favorable",min:0.38,rgba:[238,150,75,195]},{name:"Très favorable",min:0.55,rgba:[209,73,91,215]}];
function levelOf(s){return s>=LEVELS[3].min?3:s>=LEVELS[2].min?2:s>=LEVELS[1].min?1:0}
function ownOk(pub){return prefs.own==="all"||(prefs.own==="pub"?pub===1:pub!==1)}
function pixelScore(key,AE,t,w){
  const a=AE[t.cls];if(!a)return 0;
  if(prefs.own!=="all"){if(t.pub==null&&prefs.own==="pub")return 0;if(prefs.own==="pub"&&t.pub!==1)return 0;if(prefs.own==="priv"&&t.pub===1)return 0}
  return a*phFactor(t.ph,key)*lossFactor(t.loss)*combine(reliefWet(w.wet,t.rel),w.temp,w.fm,w.shock)*w.mf;
}

/* ====================================================================== Carte */
const map=L.map("map",{zoomControl:false,preferCanvas:true,attributionControl:true}).setView([prefs.view.lat,prefs.view.lon],prefs.view.z);
const geopf=(l,fmt="image/png",style="normal")=>`https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${l}&STYLE=${encodeURIComponent(style)}&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=${fmt}`;
const STYLES={pub:"FORETS PUBLIQUES ONF",roads:"normal"};
const BASES={
  sat:{name:"Satellite + chemins",layer:L.layerGroup([
    L.tileLayer(geopf("ORTHOIMAGERY.ORTHOPHOTOS","image/jpeg"),{maxZoom:19,maxNativeZoom:19,attribution:"© IGN"}),
    L.tileLayer(geopf("TRANSPORTNETWORKS.ROADS","image/png",STYLES.roads),{maxZoom:19,maxNativeZoom:18,opacity:.9,zIndex:2})])},
  plan:{name:"Plan IGN",layer:L.tileLayer(geopf("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2"),{maxZoom:19,attribution:"© IGN"})},
  osm:{name:"OpenStreetMap",layer:L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"})},
};
const OVER={
  forest:L.tileLayer.wms("https://data.geopf.fr/wms-r/wms",{layers:"LANDCOVER.FORESTINVENTORY.V2",format:"image/png",transparent:true,opacity:.55,attribution:"IGN BD Forêt",zIndex:3}),
  geol:L.tileLayer.wms("https://geoservices.brgm.fr/geologie",{layers:"SCAN_H_GEOL50",format:"image/png",transparent:true,opacity:.5,attribution:"BRGM",zIndex:4}),
  ph:L.tileLayer.wms("https://maps.isric.org/mapserv?map=/map/phh2o.map",{layers:"phh2o_0-5cm_mean",format:"image/png",transparent:true,opacity:.5,crs:L.CRS.EPSG4326,attribution:"ISRIC SoilGrids",zIndex:5}),
  cadastre:L.tileLayer(geopf("CADASTRALPARCELS.PARCELLAIRE_EXPRESS"),{minZoom:13,maxZoom:19,maxNativeZoom:19,opacity:.85,attribution:"Cadastre © IGN/DGFiP",zIndex:7}),
  pubOutline:L.tileLayer(geopf("FORETS.PUBLIQUES","image/png",STYLES.pub),{maxZoom:19,maxNativeZoom:16,opacity:.7,attribution:"Forêts publiques © ONF/IGN",zIndex:7}),
};
const tileLat=(y,z)=>{const n=Math.PI-2*Math.PI*y/(256*2**z);return 180/Math.PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n)))};
function drawZones(cv,coords){
  const key=prefs.species;if(!TERRAIN.ready||!SPECIES[key])return;
  const g=TERRAIN.meta.grid,R=TERRAIN.meta.region,AE=affEff(key);
  const N=cv.width,step=256/N,z=coords.z,world=256*2**z;
  const lonL=coords.x*256/world*360-180,lonR=(coords.x+1)*256/world*360-180;
  const latT=tileLat(coords.y*256,z),latB=tileLat((coords.y+1)*256,z);
  if(lonR<R.west||lonL>R.east||latB>R.north||latT<R.south)return;
  const G=gridFor((latT+latB)/2,(lonL+lonR)/2);if(!nodeParts(G).size)return;
  const ctx=cv.getContext("2d"),img=ctx.createImageData(N,N),d=img.data;
  const lons=new Float64Array(N);for(let px=0;px<N;px++)lons[px]=(coords.x*256+(px+0.5)*step)/world*360-180;
  const t={cls:0,rel:0,loss:0,pub:null,ph:null};let any=false;
  for(let py=0;py<N;py++){
    const lat=tileLat(coords.y*256+(py+0.5)*step,z);
    const r=Math.floor((g.north-lat)/g.dlat);if(r<0||r>=g.h)continue;
    let w=null,wLon=-999;
    for(let px=0;px<N;px++){
      const lon=lons[px];const c=Math.floor((lon-g.west)/g.dlon);if(c<0||c>=g.w)continue;
      const i=r*g.w+c;t.cls=TERRAIN.cls[i];if(!t.cls||!AE[t.cls])continue;
      if(lon-wLon>0.01||lon<wLon){w=interp(G,lat,lon);wLon=lon}
      if(!w)continue;
      t.rel=TERRAIN.rel?TERRAIN.rel[i]:0;t.loss=TERRAIN.loss?TERRAIN.loss[i]:0;t.pub=TERRAIN.pub?TERRAIN.pub[i]:null;t.ph=phLocal(lat,lon);
      const lv=levelOf(pixelScore(key,AE,t,w));if(!lv)continue;
      const col=LEVELS[lv].rgba,o=(py*N+px)*4;d[o]=col[0];d[o+1]=col[1];d[o+2]=col[2];d[o+3]=col[3];any=true;
    }
  }
  if(any)ctx.putImageData(img,0,0);
}
const ZonesLayer=L.GridLayer.extend({createTile(coords){
  const cv=document.createElement("canvas");cv.className="zones-tile";const N=coords.z>=13?256:128;cv.width=N;cv.height=N;
  try{drawZones(cv,coords)}catch(e){console.error(e)}return cv}});
const zonesLayer=new ZonesLayer({zIndex:6,updateWhenZooming:false,keepBuffer:1,attribution:"Zones : IGN, OSM, UMD, ISRIC, Open-Meteo"});
const meteoLayer=L.layerGroup(),findsLayer=L.layerGroup(),trackLayer=L.layerGroup();

/* Chemins et sentiers de l'IGN (BD TOPO), découpés en tuiles de 0,1° et affichés à partir du zoom 14 */
const PATHS={index:null,chunks:new Map(),loading:new Set(),layer:L.layerGroup(),drawn:new Set(),renderer:L.canvas({padding:0.3})};
const PATH_STYLE={
  sat:{1:{color:"#FFFFFF",weight:2.6,opacity:.9},2:{color:"#FFF3C4",weight:2.2,opacity:.95,dashArray:"7 5"},3:{color:"#FFE08A",weight:1.8,opacity:.95,dashArray:"2 5"},4:{color:"#B9F0FF",weight:1.8,opacity:.9,dashArray:"5 4"}},
  map:{1:{color:"#7A5230",weight:2.4,opacity:.85},2:{color:"#7A5230",weight:2,opacity:.85,dashArray:"7 5"},3:{color:"#7A5230",weight:1.6,opacity:.85,dashArray:"2 5"},4:{color:"#2C7FB8",weight:1.6,opacity:.8,dashArray:"5 4"}}};
async function loadPathIndex(){try{const r=await fetch("data/paths/index.json",{cache:"no-cache"});if(r.ok){PATHS.index=await r.json();PATHS.index.set=new Set(PATHS.index.tiles);updatePaths()}}catch(e){}}
function decodePaths(j){return j.l.map(a=>{const code=a[0];let x=a[1],y=a[2];const pts=[[y/1e5,x/1e5]];for(let k=3;k<a.length;k+=2){x+=a[k];y+=a[k+1];pts.push([y/1e5,x/1e5])}return [code,pts]})}
function drawChunk(key){
  if(PATHS.drawn.has(key))return;const lines=PATHS.chunks.get(key);if(!lines)return;
  const st=PATH_STYLE[prefs.base==="sat"?"sat":"map"];
  const g=L.layerGroup();for(const[code,pts]of lines)L.polyline(pts,{...st[code]||st[2],renderer:PATHS.renderer,interactive:false}).addTo(g);
  g.addTo(PATHS.layer);g._key=key;PATHS.drawn.add(key);
}
function updatePaths(){
  const on=prefs.layers.paths&&map.getZoom()>=14&&PATHS.index;
  if(!on){if(map.hasLayer(PATHS.layer))map.removeLayer(PATHS.layer);return}
  if(!map.hasLayer(PATHS.layer))PATHS.layer.addTo(map);
  const b=map.getBounds().pad(0.3),st=PATHS.index.step,need=new Set();
  for(let i=Math.floor(b.getWest()/st);i<=Math.floor(b.getEast()/st);i++)for(let j=Math.floor(b.getSouth()/st);j<=Math.floor(b.getNorth()/st);j++){const k=i+"_"+j;if(PATHS.index.set.has(k))need.add(k)}
  // Retire les tuiles éloignées pour garder la carte fluide
  PATHS.layer.eachLayer(g=>{if(!need.has(g._key)){PATHS.layer.removeLayer(g);PATHS.drawn.delete(g._key)}});
  for(const k of need){
    if(PATHS.chunks.has(k)){drawChunk(k);continue}
    if(PATHS.loading.has(k))continue;PATHS.loading.add(k);
    fetch(`data/paths/${k}.json`).then(r=>r.ok?r.json():null).then(j=>{PATHS.loading.delete(k);if(!j)return;PATHS.chunks.set(k,decodePaths(j));
      if(PATHS.chunks.size>40){const first=PATHS.chunks.keys().next().value;if(!need.has(first))PATHS.chunks.delete(first)}updatePaths()}).catch(()=>PATHS.loading.delete(k));
  }
}
function restylePaths(){PATHS.layer.clearLayers();PATHS.drawn.clear();updatePaths()}
let activeLine=null;
let currentBase=null;
function setBase(k){if(!BASES[k])k="sat";if(currentBase)map.removeLayer(currentBase);currentBase=BASES[k].layer.addTo(map);
  if(currentBase.bringToBack)currentBase.bringToBack();prefs.base=k;savePrefs();renderBaseChoices();if(typeof restylePaths==="function")restylePaths()}
function applyLayers(){
  for(const k of Object.keys(OVER))prefs.layers[k]?OVER[k].addTo(map):map.removeLayer(OVER[k]);
  prefs.layers.zones?zonesLayer.addTo(map):map.removeLayer(zonesLayer);
  prefs.layers.meteo?meteoLayer.addTo(map):map.removeLayer(meteoLayer);
  if(prefs.layers.finds){findsLayer.addTo(map);trackLayer.addTo(map)}else{map.removeLayer(findsLayer);map.removeLayer(trackLayer)}
  updatePaths();savePrefs();
}
function renderGrid(){
  meteoLayer.clearLayers();if(!SPECIES[prefs.species]||!prefs.layers.meteo)return;
  const vb=map.getBounds().pad(0.2),b={w:vb.getWest(),e:vb.getEast(),s:vb.getSouth(),n:vb.getNorth()};
  const G=gridFor((b.s+b.n)/2,(b.w+b.e)/2),S=nodeParts(G);
  for(const p of nodeKeysFor(G,b)){const r=S.get(p.key);if(!r||r.s<0.12)continue;
    L.rectangle([[p.lat-G.dlat/2,p.lon-G.dlon/2],[p.lat+G.dlat/2,p.lon+G.dlon/2]],{stroke:false,fillColor:ramp(r.s),fillOpacity:0.15+0.4*r.s,interactive:false}).addTo(meteoLayer)}
}
function refreshAll(){renderGrid();zonesLayer.redraw();updateTop();updateZoneStatus()}
let moveTimer;
map.on("moveend",()=>{const c=map.getCenter();prefs.view={lat:c.lat,lon:c.lng,z:map.getZoom()};savePrefs();updateTop();updateZoneStatus();updatePaths();
  clearTimeout(moveTimer);moveTimer=setTimeout(()=>{ensureWeather();renderGrid()},700)});

/* ====================================================================== Bandeau du haut */
function wxChips(m,r){
  return `<span class="chip">${label(r.s)}</span><span class="chip">Pluie 15 j ${fmt(m.r15)} mm</span><span class="chip">Air 7 j ${fmt(m.t7,1)} °C</span>`+
    (m.sm!=null?`<span class="chip">Sol humide ${fmt(m.sm*100)} %</span>`:"")+(m.ts!=null?`<span class="chip">Sol ${fmt(m.ts,1)} °C</span>`:"")+
    (r.shock>0.3?`<span class="chip cal">Sol qui se rafraîchit (−${fmt(m.drop,1)} °C)</span>`:"")+(r.frost?`<span class="chip warn">Gel récent</span>`:"");
}
function updateTop(){
  const key=prefs.species,S=ALL[key],P=profileFor(key);
  $("#topTitle").textContent=S.name;
  const dayTxt=["aujourd'hui","demain","après-demain","dans 3 jours"][prefs.day];
  const g=$("#gauge"),gv=$("#gaugeVal"),stats=$("#stats");
  if(!P){g.style.setProperty("--g",0);gv.textContent="–";$("#topSub").textContent="Pas de profil météo pour cette espèce";stats.innerHTML="";$("#speciesInfo").innerHTML="";return}
  const c=map.getCenter(),nd=nearestNode(c.lat,c.lng),met=nd&&nd.mets[prefs.day];
  $("#topSub").textContent=`Météo ${dayTxt}, au centre de la carte`;
  if(!met){g.style.setProperty("--g",0);gv.textContent="–";stats.innerHTML=`<span class="chip">${map.getZoom()<8?"Zoome pour charger la météo":"Chargement de la météo…"}</span>`}
  else{const r=score(met,key);const pct=Math.round(r.s*100);
    g.style.setProperty("--g",pct);g.style.setProperty("--g-col",ramp(Math.max(r.s,0.26)));gv.textContent=pct+"%";
    const AE=affEff(key),M=weatherModel(key);
    stats.innerHTML=wxChips(met,r)+(M?`<span class="chip cal">Appris sur ${M.n} cueillettes publiques</span>`:"")+
      (P.calibrated?`<span class="chip cal">Ajusté sur tes ${P.calibrated} relevés</span>`:"")+(AE&&AE.personal?`<span class="chip cal">Habitats ajustés sur tes ${AE.personal.outs} sorties</span>`:"")+
      (prefs.own!=="all"?`<span class="chip">${prefs.own==="pub"?"Forêts publiques seulement":"Privé seulement"}</span>`:"");}
  $("#speciesInfo").innerHTML=`<b>Arbres :</b> ${esc(P.hosts)} · <b>Sol :</b> ${esc(P.soil)} · <b>Saison :</b> ${esc(P.season)}`;
}
function updateZoneStatus(){
  const el=$("#zoneStatus");if(!el)return;
  if(TERRAIN.ready){const c=map.getCenter();el.textContent=terrainAt(c.lat,c.lng)?"· à 55 m près":"· hors zone détaillée"}
  else el.textContent=TERRAIN.error==="pending"?"· zones en préparation":TERRAIN.error?"· zones indisponibles":"· chargement des zones…";
}
$("#collapseBtn").onclick=()=>{prefs.collapsed=!prefs.collapsed;savePrefs();applyCollapse()};
function applyCollapse(){$("#topDetails").hidden=prefs.collapsed;$("#collapseBtn").textContent=prefs.collapsed?"Détails":"Réduire";$("#collapseBtn").setAttribute("aria-expanded",String(!prefs.collapsed))}

/* ====================================================================== Sélecteurs */
const sel=$("#speciesSelect");
sel.innerHTML=Object.entries(SPECIES).map(([k,s])=>`<option value="${k}">${esc(s.name)}</option>`).join("");
if(!SPECIES[prefs.species])prefs.species="cepe";
sel.value=prefs.species;
sel.onchange=()=>{prefs.species=sel.value;savePrefs();refreshAll()};
function renderDays(){$("#days").innerHTML=["Auj.","Demain","J+2","J+3"].map((n,i)=>`<button aria-pressed="${prefs.day===i}" data-day="${i}">${n}</button>`).join("")}
$("#days").onclick=e=>{const b=e.target.closest("[data-day]");if(!b)return;prefs.day=+b.dataset.day;savePrefs();renderDays();refreshAll();ensureWeather()};
$("#btnRefresh").onclick=()=>{toast("Mise à jour de la météo…",1500);loadMeteo();ensureWeather(true)};

/* ====================================================================== Ma position */
let meMarker=null;
function showMe(lat,lon){const icon=L.divIcon({className:"",html:'<div class="me"></div>',iconSize:[16,16]});
  if(meMarker)meMarker.setLatLng([lat,lon]);else meMarker=L.marker([lat,lon],{icon,interactive:false,zIndexOffset:1000}).addTo(map)}
$("#btnLocate").onclick=()=>{
  if(!navigator.geolocation){toast("Localisation indisponible sur cet appareil.");return}
  toast("Localisation…",1500);
  navigator.geolocation.getCurrentPosition(p=>{showMe(p.coords.latitude,p.coords.longitude);map.setView([p.coords.latitude,p.coords.longitude],Math.max(map.getZoom(),15))},
    ()=>toast("Position refusée ou introuvable. Vérifie l'autorisation de localisation."),{enableHighAccuracy:true,timeout:15000,maximumAge:30000});
};

/* ====================================================================== Lecture d'un point */
async function forestAt(lat,lon){
  const d=0.0004;
  const url=`https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=LANDCOVER.FORESTINVENTORY.V2:formation_vegetale&OUTPUTFORMAT=application/json&COUNT=3&SRSNAME=EPSG:4326&BBOX=${lat-d},${lon-d},${lat+d},${lon+d},urn:ogc:def:crs:EPSG::4326`;
  try{const j=await fetchJSON(url,10000);const f=j.features&&j.features[0];if(!f)return {type:null,essence:null};
    const p=f.properties||{};const pick=re=>{const k=Object.keys(p).find(k=>re.test(k)&&!/code/i.test(k));return k?p[k]:null};
    return {type:pick(/^tfv$/i)||pick(/tfv/i)||pick(/lib/i),essence:pick(/essence/i)}}catch(e){return undefined}
}
async function phAt(lat,lon){
  try{const j=await fetchJSON(`https://rest.isric.org/soilgrids/v2.0/properties/query?lon=${lon.toFixed(4)}&lat=${lat.toFixed(4)}&property=phh2o&depth=0-5cm&value=mean`,10000);
    const v=j.properties.layers[0].depths[0].values.mean;return v==null?null:v/10}catch(e){return undefined}
}
function wxDetails(m,r,elev){
  return `Météo : <b>${label(r.s)} (${Math.round(r.s*100)} %)</b><br>Pluie 7 j / 15 j / 30 j : ${fmt(m.r7)} / ${fmt(m.r15)} / ${fmt(m.r30)} mm<br>Air, moyenne 7 j : ${fmt(m.t7,1)} °C<br>`+
    (m.sm!=null?`Sol : ${fmt(m.sm*100)} % d'eau`+(m.ts!=null?`, ${fmt(m.ts,1)} °C`:"")+`<br>`:"")+
    (m.drop!=null&&m.drop>0.5?`Le sol s'est rafraîchi de ${fmt(m.drop,1)} °C en quelques jours<br>`:"")+
    (r.frost?`<span style="color:var(--danger)">Gel récent (${fmt(m.fmin,1)} °C)</span><br>`:"")+(elev!=null?`Altitude ≈ ${fmt(elev)} m`:"");
}
let addMode=false,placing=false;
map.on("click",async e=>{
  if(addMode||placing)return;
  const {lat,lng}=e.latlng;const key=prefs.species;
  const pop=L.popup().setLatLng(e.latlng).setContent("Lecture du terrain…").openOn(map);
  const t=terrainAt(lat,lng);
  const [f,ph]=t?[null,t.ph]:await Promise.all([forestAt(lat,lng),phAt(lat,lng)]);
  let h="",head="";
  if(t){
    if(!t.cls)h+="Pas de bois cartographié ici<br>";
    else{h+=`Peuplement : <b>${esc(className(t.cls))}</b><br>`;
      if(SPECIES[key]){const a=affEff(key)[t.cls];h+=`Affinité ${esc(ALL[key].name.toLowerCase())} : ${a>=0.8?"forte":a>=0.5?"moyenne":a>0.1?"faible":"nulle"}<br>`}}
    if(t.pub!=null)h+=t.pub?"Forêt publique (ONF)<br>":"Terrain privé (ou non soumis au régime forestier)<br>";
    if(t.rel)h+=`Relief : ${t.rel>190?"fond ou versant humide":t.rel<70?"point haut ou pente sèche":"intermédiaire"}<br>`;
    if(t.loss)h+=`Coupe ou trouée détectée en 20${String(t.loss).padStart(2,"0")}<br>`;
  }
  else if(f===undefined)h+="Forêt : lecture impossible<br>";
  else if(f&&f.type)h+=`<b>${esc(f.type)}</b><br>`+(f.essence?`Essence : ${esc(f.essence)}<br>`:"");
  else if(f)h+="Pas de forêt cartographiée ici<br>";
  if(ph!=null)h+=`pH du sol ≈ ${fmt(ph,1)} (${ph<5.5?"acide":ph<6.5?"légèrement acide":ph<7.5?"neutre":"calcaire"})<br>`;
  const nd=nearestNode(lat,lng),met=nd&&nd.mets[prefs.day];
  if(t&&t.cls&&SPECIES[key]){const G=gridFor(lat,lng),w=interp(G,lat,lng);
    if(w){const s=pixelScore(key,affEff(key),t,w);const lv=levelOf(s);
      head=`<b style="font-size:16px">${lv?LEVELS[lv].name:prefs.own!=="all"&&!ownOk(t.pub)?"Exclu par le filtre":"Peu probable"}</b> · ${Math.round(s*100)} %<br>`}}
  if(met&&SPECIES[key]){const r=score(met,key);h+=`<hr style="border:0;border-top:1px solid var(--line)">`+wxDetails(met,r,nd.elev)}
  pop.setContent(head+h||"Rien à lire ici.");
});

/* ====================================================================== Feuilles */
let openSheet=null;
function show(id){closeSheet();openSheet=$(id);openSheet.hidden=false;$("#scrim").hidden=false;openSheet.scrollTop=0}
function closeSheet(){if(openSheet){openSheet.hidden=true;if(openSheet.id==="sheetAdd"&&!placing)stopGps();openSheet=null}$("#scrim").hidden=true;if(!placing)addMode=false}
$("#scrim").onclick=()=>closeSheet();
document.querySelectorAll("[data-close]").forEach(b=>b.onclick=()=>closeSheet());

/* ====================================================================== Ajout d'une trouvaille */
const draft={species:"cepe",other:"",lat:null,lon:null,acc:null,alt:null,src:null,qty:"2-5",trees:[]};
let watchId=null;
function renderAdd(){
  $("#addSpecies").innerHTML=Object.entries(ALL).map(([k,s])=>`<button data-sp="${k}" aria-pressed="${draft.species===k}"><span class="sw" style="background:${s.color};margin-right:6px"></span>${esc(s.name)}</button>`).join("");
  $("#addOther").hidden=draft.species!=="autre";
  $("#addQty").innerHTML=QTY.map(([v,t])=>`<button data-q="${v}" aria-pressed="${draft.qty===v}">${t}</button>`).join("");
  $("#addTrees").innerHTML=TREES.map(t=>`<button data-tree="${t}" aria-pressed="${draft.trees.includes(t)}">${t}</button>`).join("");
  $("#addOuting").hidden=!active;
  renderGps();
}
function renderGps(){
  const dot=$("#gpsDot"),txt=$("#gpsText");
  if(draft.lat==null){dot.className="dot wait";txt.innerHTML="Recherche du GPS…<small>Reste à découvert quelques secondes si possible.</small>";return}
  dot.className="dot "+(draft.src==="map"||draft.acc<=30?"ok":"wait");
  txt.innerHTML=`${draft.lat.toFixed(5)}, ${draft.lon.toFixed(5)}<small>${draft.src==="map"?"Placé sur la carte":`GPS · précision ± ${Math.round(draft.acc)} m`}</small>`;
}
function startGps(){
  stopGps();if(!navigator.geolocation){$("#gpsText").textContent="GPS indisponible : place le point sur la carte.";return}
  // Pendant une sortie, on part de la dernière position connue
  if(active&&active.track.length){const p=active.track[active.track.length-1];if(Date.now()/1000-p[2]<60)Object.assign(draft,{lat:p[0],lon:p[1],acc:p[3]||20,src:"gps",t:Date.now()-5000})}
  watchId=navigator.geolocation.watchPosition(p=>{
    if(draft.src==="map")return;
    if(draft.acc==null||p.coords.accuracy<=draft.acc||Date.now()-(draft.t||0)>8000){
      Object.assign(draft,{lat:p.coords.latitude,lon:p.coords.longitude,acc:p.coords.accuracy,alt:p.coords.altitude,src:"gps",t:Date.now()});renderGps()}
  },()=>{if(draft.lat==null){$("#gpsDot").className="dot";$("#gpsText").innerHTML="GPS refusé ou indisponible<small>Autorise la localisation, ou place le point sur la carte.</small>"}},
  {enableHighAccuracy:true,timeout:30000,maximumAge:0});
  renderGps();
}
function stopGps(){if(watchId!=null){navigator.geolocation.clearWatch(watchId);watchId=null}}
$("#btnAdd").onclick=()=>{
  Object.assign(draft,{species:SPECIES[prefs.species]?prefs.species:"cepe",other:"",lat:null,lon:null,acc:null,alt:null,src:null,qty:"2-5",trees:[],t:0});
  $("#addDate").value=todayISO();const n=new Date();$("#addTime").value=String(n.getHours()).padStart(2,"0")+":"+String(n.getMinutes()).padStart(2,"0");
  $("#addNote").value="";$("#addOther").value="";
  renderAdd();show("#sheetAdd");addMode=true;startGps();
};
$("#addSpecies").onclick=e=>{const b=e.target.closest("[data-sp]");if(!b)return;draft.species=b.dataset.sp;renderAdd()};
$("#addQty").onclick=e=>{const b=e.target.closest("[data-q]");if(!b)return;draft.qty=b.dataset.q;renderAdd()};
$("#addTrees").onclick=e=>{const b=e.target.closest("[data-tree]");if(!b)return;const t=b.dataset.tree;draft.trees=draft.trees.includes(t)?draft.trees.filter(x=>x!==t):[...draft.trees,t];renderAdd()};
$("#useGps").onclick=()=>{draft.src=null;draft.acc=null;draft.lat=null;startGps()};
$("#useMap").onclick=()=>{
  placing=true;$("#sheetAdd").hidden=true;$("#scrim").hidden=true;$("#bottom").hidden=true;$("#crosshair").hidden=false;$("#placeBar").hidden=false;
  if(draft.lat!=null)map.setView([draft.lat,draft.lon],Math.max(map.getZoom(),16));
};
$("#placeOk").onclick=()=>{
  const c=map.getCenter();Object.assign(draft,{lat:c.lat,lon:c.lng,acc:null,alt:null,src:"map"});
  placing=false;$("#crosshair").hidden=true;$("#placeBar").hidden=true;$("#bottom").hidden=false;$("#sheetAdd").hidden=false;$("#scrim").hidden=false;renderGps();
};
$("#addSave").onclick=()=>{
  if(draft.lat==null){toast("Il manque la position : attends le GPS ou place le point sur la carte.");return}
  const date=$("#addDate").value||todayISO();
  if(date>todayISO()){toast("La date est dans le futur.");return}
  const f={id:uid(),species:draft.species,other:draft.species==="autre"?$("#addOther").value.trim():"",date,time:$("#addTime").value||"",
    lat:+draft.lat.toFixed(6),lon:+draft.lon.toFixed(6),acc:draft.acc?Math.round(draft.acc):null,gpsAlt:draft.alt!=null?Math.round(draft.alt):null,src:draft.src,
    qty:draft.qty,trees:[...draft.trees],note:$("#addNote").value.trim(),forest:null,ph:null,wx:null,created:new Date().toISOString(),
    outingId:active&&date===todayISO()?active.id:null};
  if(f.outingId){active.findIds.push(f.id);if(!active.species.includes(f.species)&&SPECIES[f.species])active.species.push(f.species);saveActive()}
  finds.unshift(f);saveFinds();renderFindMarkers();stopGps();closeSheet();
  toast("Trouvaille enregistrée. Ajout de la météo des jours précédents…",3500);
  enrich(f).then(ok=>{if(ok)toast("Météo, sol et forêt ajoutés à ta trouvaille.")});
};

/* ====================================================================== Météo d'une trouvaille ou d'une sortie */
async function wxFor(lat,lon,date){
  const age=dayDiff(date,todayISO());let wx,t;
  if(age<=60){wx=(await forecastWx([{lat,lon}],Math.max(PAST,age+PAST),1))[0];t=wx.days.findIndex(d=>d.d===date)}
  else{wx=await archiveWx({lat,lon},addDays(date,-PAST),date);t=wx.days.length-1}
  if(t<0)throw new Error("date");
  const days=wx.days.slice(Math.max(0,t-PAST),t+1).map(d=>({d:d.d,p:round1(d.p),tmin:round1(d.tmin),tmax:round1(d.tmax),tmean:round1(d.tmean),sm:d.sm!=null?Math.round(d.sm*1000)/1000:null,ts:round1(d.ts)}));
  const m=metrics(days,days.length-1);
  return {elev:wx.elev,days,m:{r30:round1(m.r30),er:round1(m.er),r15:round1(m.r15),r7:round1(m.r7),t7:round1(m.t7),fmin:round1(m.fmin),sm:m.sm,ts:round1(m.ts),drop:round1(m.drop)}};
}
function stampTerrain(o){const t=terrainAt(o.lat,o.lon);if(t){o.cls=t.cls;o.rel=t.rel;o.pub=t.pub;o.loss=t.loss}}
async function enrich(f){
  let ok=true;
  if(!f.wx){try{f.wx=await wxFor(f.lat,f.lon,f.date)}catch(e){ok=false}}
  if(f.forest==null){const r=await forestAt(f.lat,f.lon);if(r!==undefined)f.forest=r;else ok=false}
  if(f.ph==null){const loc=phLocal(f.lat,f.lon);if(loc!=null)f.ph=loc;else{const r=await phAt(f.lat,f.lon);if(r!==undefined)f.ph=r}}
  if(f.cls==null)stampTerrain(f);
  const i=finds.findIndex(x=>x.id===f.id);if(i>=0){finds[i]=f;saveFinds();renderFindMarkers();if(!$("#sheetFinds").hidden)renderFinds();refreshAll()}
  if(!ok&&!f.wx)toast("Météo non récupérée (réseau ?). Nouvel essai à la prochaine ouverture.");
  return ok;
}
async function enrichOuting(o){
  if(o.wx||!o.track||!o.track.length)return;
  const p=o.track[Math.floor(o.track.length/2)];
  try{const w=await wxFor(p[0],p[1],o.date);o.wx={elev:w.elev,m:w.m}}catch(e){return}
  const i=outings.findIndex(x=>x.id===o.id);if(i>=0){outings[i]=o;saveOutings();refreshAll()}
}

/* ====================================================================== Sorties */
let outWatch=null,wakeLock=null,pillTimer=null;
const outDist=o=>{let d=0;for(let i=1;i<o.track.length;i++)d+=haversine(o.track[i-1],o.track[i]);return d};
const durTxt=(a,b)=>{const m=Math.max(0,Math.round((b-a)/60000));return m<60?`${m} min`:`${Math.floor(m/60)} h ${String(m%60).padStart(2,"0")}`};
const kmTxt=m=>m<1000?`${Math.round(m)} m`:`${fmt(m/1000,1)} km`;
async function keepAwake(){try{if("wakeLock" in navigator&&document.visibilityState==="visible")wakeLock=await navigator.wakeLock.request("screen")}catch(e){}}
document.addEventListener("visibilitychange",()=>{if(active&&document.visibilityState==="visible")keepAwake()});
function startTracking(){
  if(!navigator.geolocation){toast("GPS indisponible sur cet appareil.");return}
  stopTracking(true);
  outWatch=navigator.geolocation.watchPosition(p=>{
    if(!active)return;const c=p.coords;if(c.accuracy>45)return;
    const pt=[+c.latitude.toFixed(6),+c.longitude.toFixed(6),Math.round(Date.now()/1000),Math.round(c.accuracy)];
    const last=active.track[active.track.length-1];
    if(last&&haversine(last,pt)<15)return;
    active.track.push(pt);saveActive();drawActive();showMe(pt[0],pt[1]);
  },()=>toast("GPS refusé ou indisponible : la sortie n'enregistre pas ton parcours."),{enableHighAccuracy:true,maximumAge:5000,timeout:60000});
  keepAwake();
  clearInterval(pillTimer);pillTimer=setInterval(renderPill,20000);
}
function stopTracking(silent){if(outWatch!=null){navigator.geolocation.clearWatch(outWatch);outWatch=null}clearInterval(pillTimer);try{wakeLock&&wakeLock.release()}catch(e){}wakeLock=null}
function drawActive(){
  if(!active){if(activeLine){map.removeLayer(activeLine);activeLine=null}renderPill();return}
  const ll=active.track.map(p=>[p[0],p[1]]);
  if(!activeLine)activeLine=L.polyline(ll,{color:"#2F7DE1",weight:4,opacity:.9}).addTo(map);else activeLine.setLatLngs(ll);
  renderPill();
}
function renderPill(){
  const p=$("#outingPill");p.hidden=!active;document.body.classList.toggle("outing",!!active);if(!active)return;
  $("#outingText").textContent=`Sortie en cours · ${kmTxt(outDist(active))} · ${durTxt(active.startTs,Date.now())} · ${active.findIds.length} trouvaille${active.findIds.length>1?"s":""}`;
}
const osSel=new Set();
$("#btnOuting").onclick=()=>{
  if(active){openEnd();return}
  osSel.clear();if(SPECIES[prefs.species])osSel.add(prefs.species);renderOsSpecies();show("#sheetOutingStart");
};
function renderOsSpecies(){$("#osSpecies").innerHTML=Object.entries(SPECIES).map(([k,s])=>`<button data-sp="${k}" aria-pressed="${osSel.has(k)}"><span class="sw" style="background:${s.color};margin-right:6px"></span>${esc(s.name)}</button>`).join("")}
$("#osSpecies").onclick=e=>{const b=e.target.closest("[data-sp]");if(!b)return;const k=b.dataset.sp;osSel.has(k)?osSel.delete(k):osSel.add(k);renderOsSpecies()};
$("#osStart").onclick=()=>{
  if(!osSel.size){toast("Choisis au moins une espèce cherchée.");return}
  active={id:uid(),date:todayISO(),startTs:Date.now(),species:[...osSel],track:[],findIds:[]};
  saveActive();closeSheet();startTracking();drawActive();
  toast("Sortie démarrée : garde l'app ouverte pour suivre ton parcours.",4000);
};
$("#outingEnd").onclick=()=>openEnd();
let oeRatings={},oeDiscard=false;
function openEnd(){
  if(!active)return;
  oeDiscard=false;oeRatings={};
  const spp=[...new Set([...active.species,...finds.filter(f=>active.findIds.includes(f.id)&&SPECIES[f.species]).map(f=>f.species)])];
  for(const k of spp){const fs=finds.filter(f=>active.findIds.includes(f.id)&&f.species===k);
    oeRatings[k]=!fs.length?0:fs.some(f=>f.qty==="16+")?3:fs.length>=3||fs.some(f=>f.qty==="6-15")?2:1}
  $("#oeNote").value="";renderEnd();show("#sheetOutingEnd");
}
function renderEnd(){
  $("#oeSummary").innerHTML=`<span class="chip">${dateFR(active.date,{weekday:"long",day:"numeric",month:"long"})}</span><span class="chip">${durTxt(active.startTs,Date.now())}</span><span class="chip">${kmTxt(outDist(active))}</span><span class="chip">${active.findIds.length} trouvaille${active.findIds.length>1?"s":""} notée${active.findIds.length>1?"s":""}</span>`;
  $("#oeRatings").innerHTML=Object.keys(oeRatings).map(k=>`<div class="rating-row" data-sp="${k}"><b><span class="sw" style="background:${ALL[k].color}"></span>${esc(ALL[k].name)}</b>
    <div class="choices rating">${RATINGS.map((r,i)=>`<button data-r="${i}" aria-pressed="${oeRatings[k]===i}" class="r${i}">${r}</button>`).join("")}</div></div>`).join("");
  $("#oeDiscard").textContent=oeDiscard?"Confirmer : supprimer cette sortie":"Annuler la sortie";
}
$("#oeRatings").onclick=e=>{const b=e.target.closest("[data-r]");if(!b)return;oeRatings[b.closest("[data-sp]").dataset.sp]=+b.dataset.r;renderEnd()};
$("#oeSave").onclick=()=>{
  const o={id:active.id,date:active.date,start:new Date(active.startTs).toISOString(),end:new Date().toISOString(),species:Object.keys(oeRatings),
    ratings:{...oeRatings},findIds:active.findIds,track:active.track,dist:Math.round(outDist(active)),note:$("#oeNote").value.trim(),wx:null};
  outings.unshift(o);saveOutings();active=null;saveActive();stopTracking();drawActive();renderTracks();closeSheet();
  toast("Sortie enregistrée. Merci, ça nourrit la carte.");enrichOuting(o);
};
$("#oeDiscard").onclick=()=>{
  if(!oeDiscard){oeDiscard=true;renderEnd();return}
  for(const f of finds)if(f.outingId===active.id)f.outingId=null;saveFinds();
  active=null;saveActive();stopTracking();drawActive();closeSheet();toast("Sortie annulée. Les trouvailles notées sont conservées.");
};
$("#oeContinue").onclick=()=>closeSheet();
const bestRating=o=>Math.max(0,...Object.values(o.ratings||{}));
const RCOL=["#7E8A80","#E9C46A","#F4A261","#E76F51","#B5179E"];
function renderTracks(){
  trackLayer.clearLayers();
  for(const o of outings){if(!o.track||o.track.length<2)continue;
    L.polyline(o.track.map(p=>[p[0],p[1]]),{color:RCOL[bestRating(o)],weight:3,opacity:.75,dashArray:bestRating(o)?null:"4 6"})
      .bindPopup(()=>`<b>Sortie du ${dateFR(o.date)}</b><br>${kmTxt(o.dist||0)} · ${durTxt(new Date(o.start),new Date(o.end))}<br>`+
        Object.entries(o.ratings||{}).map(([k,r])=>`${esc(ALL[k]?ALL[k].name:k)} : ${RATINGS[r]}`).join("<br>")).addTo(trackLayer)}
}

/* ====================================================================== Trouvailles : carte et liste */
function spName(f){return f.species==="autre"&&f.other?f.other:(ALL[f.species]||ALL.autre).name}
function findPopup(f){
  return `<b>${esc(spName(f))}</b><br>${dateFR(f.date,{day:"numeric",month:"long",year:"numeric"})}`+
    (f.wx?`<br>Pluie 15 j : ${fmt(f.wx.m.r15)} mm · air 7 j : ${fmt(f.wx.m.t7,1)} °C`:"")+(f.trees&&f.trees.length?`<br>${esc(f.trees.join(", "))}`:"")+(f.note?`<br><i>${esc(f.note)}</i>`:"")}
function renderFindMarkers(){
  findsLayer.clearLayers();
  for(const f of finds){const S=ALL[f.species]||ALL.autre;
    L.circleMarker([f.lat,f.lon],{radius:7,color:"#fff",weight:2,fillColor:S.color,fillOpacity:1}).bindPopup(()=>findPopup(f)).addTo(findsLayer)}
}
function updateCount(){const b=$("#findCount");b.hidden=!finds.length;b.textContent=finds.length}
function rainBars(days){
  const max=Math.max(10,...days.map(x=>x.p||0)),w=100/days.length;
  return `<svg class="rainbars" viewBox="0 0 100 38" preserveAspectRatio="none" role="img" aria-label="Pluie des ${days.length} jours précédents">`+
    days.map((x,i)=>{const h=(x.p||0)/max*28;return `<rect x="${i*w+0.4}" y="${30-h}" width="${w-0.8}" height="${Math.max(h,0.6)}" fill="${i>=days.length-16&&i<days.length-1?"#4F8FC0":"#9DB7CB"}"/>`}).join("")+
    `<line x1="0" y1="30.5" x2="100" y2="30.5" stroke="var(--line)" stroke-width=".6"/></svg>
    <div class="rainlegend"><span>J-${days.length-1}</span><span>pluie (bleu foncé : 15 derniers jours)</span><span>jour J</span></div>`;
}
let pendingDelete=null,tab="finds";
function renderFinds(){
  $("#tabFinds").setAttribute("aria-pressed",tab==="finds");$("#tabOutings").setAttribute("aria-pressed",tab==="outings");
  $("#tabFinds").textContent=`Trouvailles (${finds.length})`;$("#tabOutings").textContent=`Sorties (${outings.length})`;
  if(tab==="outings")return renderOutingList();
  const counts={};finds.forEach(f=>{const n=spName(f);counts[n]=(counts[n]||0)+1});
  $("#findSummary").innerHTML=Object.entries(counts).map(([n,c])=>`<span class="chip">${esc(n)} · ${c}</span>`).join("");
  if(!finds.length){$("#findList").innerHTML=`<div class="empty"><b>Aucune trouvaille pour l'instant</b>Touche le bouton + quand tu trouves un champignon : sa position et la météo du mois d'avant sont gardées ici.</div>`;return}
  $("#findList").innerHTML=finds.map(f=>{const S=ALL[f.species]||ALL.autre;
    const facts=[];
    if(f.wx){facts.push(`Pluie 15 j ${fmt(f.wx.m.r15)} mm`,`Air 7 j ${fmt(f.wx.m.t7,1)} °C`);if(f.wx.m.sm!=null)facts.push(`Sol humide ${fmt(f.wx.m.sm*100)} %`);if(f.wx.m.ts!=null)facts.push(`Sol ${fmt(f.wx.m.ts,1)} °C`);facts.push(`${fmt(f.wx.elev)} m`)}
    else facts.push("Météo en attente");
    if(f.ph!=null)facts.push(`pH ${fmt(f.ph,1)}`);
    if(f.cls)facts.push(esc(className(f.cls)));
    if(f.pub!=null)facts.push(f.pub?"Forêt publique":"Privé");
    if(f.qty)facts.push((QTY.find(q=>q[0]===f.qty)||[,f.qty])[1]+" pied(s)");
    if(f.outingId)facts.push("Pendant une sortie");
    return `<article class="find" data-id="${f.id}">
      <div class="find-head"><span class="sw" style="background:${S.color}"></span><b>${esc(spName(f))}</b><time>${dateFR(f.date)}${f.time?" · "+esc(f.time):""}</time></div>
      <div class="facts">${facts.map(x=>`<span class="chip">${x}</span>`).join("")}</div>
      ${f.forest&&f.forest.type?`<div class="hint">${esc(f.forest.type)}${f.forest.essence?" · "+esc(f.forest.essence):""}</div>`:""}
      ${f.trees&&f.trees.length?`<div class="hint">Arbres notés : ${esc(f.trees.join(", "))}</div>`:""}
      ${f.note?`<div>${esc(f.note)}</div>`:""}
      ${f.wx&&f.wx.days?rainBars(f.wx.days):""}
      <div class="actions">
        <button class="btn small" data-act="go">Voir sur la carte</button>
        ${!f.wx?`<button class="btn small" data-act="retry">Récupérer la météo</button>`:""}
        ${pendingDelete===f.id?`<button class="btn small danger" data-act="del2">Confirmer la suppression</button><button class="btn small" data-act="cancel">Annuler</button>`:`<button class="btn small danger" data-act="del">Supprimer</button>`}
      </div></article>`}).join("");
}
function renderOutingList(){
  const tot=outings.reduce((a,o)=>a+(o.dist||0),0);
  $("#findSummary").innerHTML=outings.length?`<span class="chip">${kmTxt(tot)} parcourus</span><span class="chip">${outings.filter(o=>bestRating(o)===0).length} sortie(s) sans rien</span>`:"";
  if(!outings.length){$("#findList").innerHTML=`<div class="empty"><b>Aucune sortie enregistrée</b>Touche le bouton « sortie » (les empreintes) en arrivant au bois : ton parcours est suivi, et le bilan de fin, même « rien », aide la carte à apprendre.</div>`;return}
  $("#findList").innerHTML=outings.map(o=>{
    const nF=finds.filter(f=>f.outingId===o.id).length;
    return `<article class="find" data-oid="${o.id}">
      <div class="find-head"><span class="sw" style="background:${RCOL[bestRating(o)]}"></span><b>Sortie</b><time>${dateFR(o.date)}</time></div>
      <div class="facts"><span class="chip">${durTxt(new Date(o.start),new Date(o.end))}</span><span class="chip">${kmTxt(o.dist||0)}</span><span class="chip">${nF} trouvaille${nF>1?"s":""}</span>
        ${o.wx?`<span class="chip">Pluie 15 j ${fmt(o.wx.m.r15)} mm</span><span class="chip">Air 7 j ${fmt(o.wx.m.t7,1)} °C</span>`:`<span class="chip">Météo en attente</span>`}</div>
      <div class="facts">${Object.entries(o.ratings||{}).map(([k,r])=>`<span class="chip rate r${r}">${esc(ALL[k]?ALL[k].name:k)} : ${RATINGS[r]}</span>`).join("")}</div>
      ${o.note?`<div>${esc(o.note)}</div>`:""}
      <div class="actions"><button class="btn small" data-oact="go">Voir le parcours</button>
        ${pendingDelete===o.id?`<button class="btn small danger" data-oact="del2">Confirmer la suppression</button><button class="btn small" data-oact="cancel">Annuler</button>`:`<button class="btn small danger" data-oact="del">Supprimer</button>`}</div>
    </article>`}).join("");
}
$("#tabFinds").onclick=()=>{tab="finds";pendingDelete=null;renderFinds()};
$("#tabOutings").onclick=()=>{tab="outings";pendingDelete=null;renderFinds()};
$("#findList").onclick=e=>{
  const ob=e.target.closest("[data-oact]");
  if(ob){const id=ob.closest("[data-oid]").dataset.oid;const o=outings.find(x=>x.id===id);if(!o)return;const a=ob.dataset.oact;
    if(a==="go"){closeSheet();if(o.track&&o.track.length>1)map.fitBounds(L.latLngBounds(o.track.map(p=>[p[0],p[1]])).pad(0.2));if(!prefs.layers.finds){prefs.layers.finds=true;applyLayers()}}
    if(a==="del"){pendingDelete=id;renderFinds()}
    if(a==="cancel"){pendingDelete=null;renderFinds()}
    if(a==="del2"){outings=outings.filter(x=>x.id!==id);for(const f of finds)if(f.outingId===id)f.outingId=null;saveFinds();pendingDelete=null;saveOutings();renderTracks();renderFinds();refreshAll();toast("Sortie supprimée.")}
    return}
  const b=e.target.closest("[data-act]");if(!b)return;const id=b.closest("[data-id]").dataset.id;const f=finds.find(x=>x.id===id);if(!f)return;
  const a=b.dataset.act;
  if(a==="go"){closeSheet();map.setView([f.lat,f.lon],17);if(!prefs.layers.finds){prefs.layers.finds=true;applyLayers()}}
  if(a==="retry"){b.disabled=true;b.textContent="Récupération…";enrich(f)}
  if(a==="del"){pendingDelete=id;renderFinds()}
  if(a==="cancel"){pendingDelete=null;renderFinds()}
  if(a==="del2"){finds=finds.filter(x=>x.id!==id);pendingDelete=null;saveFinds();renderFindMarkers();renderFinds();refreshAll();toast("Trouvaille supprimée.")}
};
$("#btnFinds").onclick=()=>{pendingDelete=null;renderFinds();show("#sheetFinds")};

/* ====================================================================== Export / import */
function download(name,text,type){const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([text],{type}));a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},1000)}
const csvQ=v=>{v=v==null?"":String(v);return /[;"\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v};
$("#exportJson").onclick=()=>download(`sous-bois-${todayISO()}.json`,JSON.stringify({app:"sous-bois",version:2,exported:new Date().toISOString(),finds,outings},null,1),"application/json");
$("#exportCsv").onclick=()=>{
  const head=["date","heure","espece","lat","lon","precision_m","altitude_m","pluie_7j_mm","pluie_15j_mm","pluie_30j_mm","tmoy_7j_c","tmin_6j_c","humidite_sol","temp_sol_c","baisse_temp_sol_c","ph","peuplement","foret_publique","relief_humidite","coupe_annee","arbres","quantite","sortie","note"];
  const rows=finds.map(f=>{const m=f.wx?f.wx.m:{};return [f.date,f.time,spName(f),f.lat,f.lon,f.acc,f.wx&&f.wx.elev,m.r7,m.r15,m.r30,m.t7,m.fmin,m.sm,m.ts,m.drop,f.ph,f.cls?className(f.cls):"",f.pub==null?"":f.pub?"oui":"non",f.rel||"",f.loss?2000+f.loss:"",(f.trees||[]).join(", "),f.qty,f.outingId||"",f.note].map(csvQ).join(";")});
  download(`sous-bois-trouvailles-${todayISO()}.csv`,"﻿"+[head.join(";"),...rows].join("\n"),"text/csv");
};
$("#exportOutCsv").onclick=()=>{
  const spp=Object.keys(SPECIES);
  const head=["date","debut","fin","distance_m","nb_trouvailles",...spp.map(k=>"bilan_"+k),"pluie_15j_mm","tmoy_7j_c","humidite_sol","temp_sol_c","note"];
  const rows=outings.map(o=>{const m=o.wx?o.wx.m:{};return [o.date,o.start,o.end,o.dist,finds.filter(f=>f.outingId===o.id).length,...spp.map(k=>o.ratings&&o.ratings[k]!=null?RATINGS[o.ratings[k]]:""),m.r15,m.t7,m.sm,m.ts,o.note].map(csvQ).join(";")});
  download(`sous-bois-sorties-${todayISO()}.csv`,"﻿"+[head.join(";"),...rows].join("\n"),"text/csv");
};
// Export anonyme : ce qui sert à apprendre, sans rien qui dise où (ni coordonnées, ni parcours, ni jour, ni notes)
function anonMetrics(m){if(!m)return null;const r=(v,st)=>v==null?null:Math.round(v/st)*st;
  return {er:r(m.er,5),r7:r(m.r7,5),r15:r(m.r15,5),r30:r(m.r30,5),t7:r(m.t7,1),fmin:r(m.fmin,1),sm:r(m.sm,0.02),ts:r(m.ts,1),drop:r(m.drop,0.5)}}
const relBucket=v=>v?Math.min(5,1+Math.floor(v/51.2)):null;   // 1 point haut sec → 5 fond humide
function anonRecords(){
  const out=[];
  for(const f of finds){if(!SPECIES[f.species]||!f.wx)continue;const t=terrainAt(f.lat,f.lon);
    out.push({type:"trouvaille",sp:f.species,mois:f.date.slice(0,7),qty:f.qty,m:anonMetrics(f.wx.m),cls:t?t.cls:f.cls??null,rel:relBucket(t?t.rel:f.rel),ph:t&&t.ph!=null?Math.round(t.ph*2)/2:f.ph!=null?Math.round(f.ph*2)/2:null})}
  for(const o of outings){if(!o.wx||!o.ratings)continue;
    let share=null;
    if(TERRAIN.ready&&o.track&&o.track.length>1){const c=new Array(16).fill(0);let n=0;for(const i of trackCells(o)){const k=TERRAIN.cls[i];if(k){c[k]++;n++}}
      if(n)share=Object.fromEntries(c.map((v,k)=>[k,Math.round(v/n*10)/10]).filter(([k,v])=>v>0))}
    for(const[sp,r]of Object.entries(o.ratings))if(SPECIES[sp])out.push({type:"sortie",sp,mois:o.date.slice(0,7),bilan:r,m:anonMetrics(o.wx.m),bois:share})}
  // Ordre mélangé pour ne pas trahir la chronologie
  for(let i=out.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[out[i],out[j]]=[out[j],out[i]]}
  return out;
}
$("#exportAnon").onclick=()=>{
  const rec=anonRecords();
  if(!rec.length){toast("Rien à partager pour l'instant : note des trouvailles ou des sorties d'abord (la météo doit être arrivée).");return}
  download(`sous-bois-anonyme-${todayISO().slice(0,7)}.json`,JSON.stringify({app:"sous-bois",type:"anonyme",version:1,cree:todayISO().slice(0,7),records:rec}),"application/json");
  toast(`${rec.length} relevés exportés, sans aucune position. Envoie le fichier pour l'entraînement.`,4000);
};
$("#importBtn").onclick=()=>$("#importFile").click();
$("#importFile").onchange=async e=>{
  const file=e.target.files[0];if(!file)return;
  try{const j=JSON.parse(await file.text());const list=Array.isArray(j)?j:j.finds;if(!Array.isArray(list))throw 0;
    const ids=new Set(finds.map(f=>f.id));let n=0,no=0;
    for(const f of list){if(f&&f.id&&typeof f.lat==="number"&&typeof f.lon==="number"&&!ids.has(f.id)){f.trees=f.trees||[];finds.push(f);n++}}
    const oids=new Set(outings.map(o=>o.id));
    for(const o of (j.outings||[])){if(o&&o.id&&!oids.has(o.id)){outings.push(o);no++}}
    finds.sort((a,b)=>(b.date+b.time).localeCompare(a.date+a.time));outings.sort((a,b)=>String(b.start).localeCompare(String(a.start)));
    saveFinds();saveOutings();renderFindMarkers();renderTracks();renderFinds();refreshAll();
    toast(n||no?`${n} trouvaille(s) et ${no} sortie(s) importée(s).`:"Rien de nouveau dans ce fichier.");
  }catch(err){toast("Fichier illisible : choisis un export JSON de Sous-Bois.")}
  e.target.value="";
};

/* ====================================================================== Couches */
const SW={zones:"#lyZones",paths:"#lyPaths",meteo:"#lyMeteo",forest:"#lyForest",geol:"#lyGeol",ph:"#lyPh",finds:"#lyFinds",cadastre:"#lyCadastre",pubOutline:"#lyPub"};
function syncLayerSwitches(){for(const[k,s]of Object.entries(SW))$(s).checked=!!prefs.layers[k]}
for(const[k,s]of Object.entries(SW))$(s).onchange=e=>{prefs.layers[k]=e.target.checked;applyLayers();if(k==="meteo")renderGrid()};
function renderBaseChoices(){$("#baseChoices").innerHTML=Object.entries(BASES).map(([k,b])=>`<button data-base="${k}" aria-pressed="${prefs.base===k}">${b.name}</button>`).join("")}
$("#baseChoices").onclick=e=>{const b=e.target.closest("[data-base]");if(b)setBase(b.dataset.base)};
const OWN=[["all","Toutes"],["pub","Forêts publiques"],["priv","Privé"]];
function renderOwn(){
  $("#ownChoices").innerHTML=OWN.map(([k,t])=>`<button data-own="${k}" aria-pressed="${prefs.own===k}">${t}</button>`).join("");
  $("#ownHint").textContent=TERRAIN.ready&&!TERRAIN.pub?"La carte des forêts publiques n'est pas encore prête : le filtre s'appliquera dès qu'elle le sera.":
    "En forêt privée, les champignons appartiennent au propriétaire : il faut son accord. En forêt publique, la cueillette familiale est en général tolérée, dans la limite des règles locales (quantités, arrêtés).";
}
$("#ownChoices").onclick=e=>{const b=e.target.closest("[data-own]");if(!b)return;prefs.own=b.dataset.own;savePrefs();renderOwn();refreshAll()};
$("#btnLayers").onclick=()=>{syncLayerSwitches();renderBaseChoices();renderOwn();show("#sheetLayers")};

/* ====================================================================== Comment c'est calculé */
async function loadModel(){try{const r=await fetch("data/model.json",{cache:"no-cache"});if(r.ok){MODEL=await r.json();calVersion++;refreshAll()}}catch(e){}}
function renderInfo(){
  const M=TERRAIN.meta,G=GRIDS.static;
  const when=iso=>iso?new Date(iso).toLocaleString("fr-FR",{day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"}):"–";
  const sp=Object.entries(SPECIES).map(([k,s])=>{const E=MODEL&&MODEL.species&&MODEL.species[k];const W=E&&E.weather,Hb=E&&E.habitat;
    const used=weatherModel(k);
    return `<tr><td>${esc(s.name)}</td><td>${E?fmt(E.n_obs):"–"}</td><td>${W&&W.auc!=null?fmt(W.auc,2):"–"}${W?used?"":" (non utilisé)":""}</td><td>${Hb&&Hb.N?fmt(Hb.N):"–"}</td><td>${personalPresences(k).length}</td></tr>`}).join("");
  $("#infoBody").innerHTML=`
  <p>Pour chaque carré de 55 m, la carte multiplie :</p>
  <ol class="info-list">
    <li><b>Le peuplement</b> : essences de la BD Forêt de l'IGN, plus les haies et petits bois de la BD TOPO. Les quartiers, zones d'activités et parcs (OpenStreetMap) sont retirés.</li>
    <li><b>Le sol</b> : pH (ISRIC SoilGrids) comparé aux préférences de l'espèce.</li>
    <li><b>Le relief</b> : fonds de vallon et versants nord gardent l'humidité, les points hauts sèchent vite${M&&M.relief_source?` (${esc(M.relief_source)})`:""}.</li>
    <li><b>Les coupes</b> : trouées et coupes rases vues par satellite depuis 2001 (Université du Maryland), pénalisées pendant une dizaine d'années.</li>
    <li><b>La météo</b> : pluie des 30 derniers jours, humidité et température du sol, baisse de température qui déclenche les poussées, gel.</li>
  </ol>
  <table class="info-table"><thead><tr><th>Espèce</th><th>Obs. publiques</th><th>Fiabilité météo</th><th>Obs. dans la zone</th><th>Tes relevés</th></tr></thead><tbody>${sp}</tbody></table>
  <p class="hint">La fiabilité va de 0,5 (hasard) à 1 (parfait) : c'est la capacité du modèle à distinguer un jour de poussée d'un jour ordinaire, mesurée sur des observations qu'il n'a pas vues. Il n'est utilisé qu'au-dessus de 0,6.</p>
  <div class="facts">
    <span class="chip">Terrain : ${M?when(M.generated):"–"}</span>
    <span class="chip">Météo fine : ${G?when(G.generated):STATUS.meteo==="ancienne"?"pas encore mise à jour aujourd'hui":"en direct"}</span>
    <span class="chip">Modèle : ${MODEL?when(MODEL.generated):"en préparation"}</span>
    <span class="chip">Tes sorties : ${outings.length}, dont ${outings.filter(o=>bestRating(o)===0).length} sans rien</span>
  </div>
  <p class="hint">Sources : IGN (BD Forêt, BD TOPO, RGE ALTI, photos aériennes, cadastre), ONF (forêts publiques), OpenStreetMap, Hansen et al. / UMD (Global Forest Change), ISRIC SoilGrids, Open-Meteo, GBIF.</p>`;
}
$("#btnInfo").onclick=()=>{renderInfo();show("#sheetInfo")};

/* ====================================================================== Démarrage */
setBase(prefs.base);applyLayers();
renderDays();applyCollapse();renderFindMarkers();renderTracks();updateCount();updateTop();updateZoneStatus();
loadTerrain();loadMeteo().then(()=>ensureWeather());loadModel();loadPathIndex();
if(active){startTracking();drawActive();toast("Sortie en cours reprise.")}
setTimeout(async()=>{for(const f of finds.filter(f=>!f.wx).slice(0,5))await enrich(f);for(const o of outings.filter(o=>!o.wx).slice(0,3))await enrichOuting(o)},3000);
window.addEventListener("online",()=>{for(const f of finds.filter(f=>!f.wx).slice(0,5))enrich(f)});
