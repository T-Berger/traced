const C={ink:'#1E2A22',soft:'#5B665D',green:'#2F5233',greenBg:'#EAF1EA',greenLine:'#C3D8C6',amber:'#B8651E',amberBg:'#FCEFE3',amberLine:'#EBB98B',line:'#DDE3DC',paper:'#F6F7F3',card:'#FFFFFF',grey:'#9AA39B',greyBg:'#E7EAE5',red:'#C2412D'};
const clamp=(x,a=0,b=1)=>Math.min(b,Math.max(a,x));
const ease=x=>x<.5?4*x*x*x:1-Math.pow(-2*x+2,3)/2;
const lin=(t,a,b)=>clamp((t-a)/(b-a));
const seg=(t,a,b)=>ease(lin(t,a,b));
const lerp=(a,b,p)=>a+(b-a)*p;
const NS='http://www.w3.org/2000/svg';
function el(tag,attrs,parent){const e=document.createElementNS(NS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e}
function T(p,x,y,s,o={}){const e=el('text',{x,y,'font-size':o.size||28,'font-weight':o.weight||500,fill:o.fill||C.ink,'text-anchor':o.anchor||'start'},p);e.textContent=s;return e}
function rng(seed){let s=seed>>>0;return()=>{s=(Math.imul(s,1664525)+1013904223)>>>0;return s/4294967296}}
function gauss(R){return (R()+R()+R()+R()-2)/0.58}
function chipState(t,seq,fd=.6){let i=0;for(let k=0;k<seq.length;k++)if(t>=seq[k].t)i=k;
 const p=clamp((t-seq[i].t)/fd);if(i===0||p>=1)return{s:seq[i],sx:1};
 const q=ease(p);return q<.5?{s:seq[i-1],sx:Math.cos(Math.PI*q)}:{s:seq[i],sx:-Math.cos(Math.PI*q)}}
function run(D,render){window.DUR=D;window.setT=t=>render(t);
 if(new URLSearchParams(location.search).has('static')){render(0);return}
 const t0=performance.now();(function f(){render(((performance.now()-t0)/1000)%D);requestAnimationFrame(f)})()}
function bg(svg){el('rect',{width:1280,height:720,fill:C.paper},svg)}
