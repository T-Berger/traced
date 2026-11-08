// Usage: node export.mjs <name> preview t1 t2 ...   -> PNG stills
//        node export.mjs <name> video              -> <name>.mp4 + <name>.gif (needs ffmpeg)
import {createRequire} from 'module';import {execSync} from 'child_process';import fs from 'fs';import path from 'path';
const require=createRequire('/Users/kiko/Desktop/inno/presentations/package.json');
const puppeteer=require('puppeteer-core');
const [name,mode,...ts]=process.argv.slice(2);
const exe=execSync('ls -d $HOME/.cache/puppeteer/chrome-headless-shell/*/*/chrome-headless-shell').toString().trim();
const here=path.dirname(new URL(import.meta.url).pathname);
const out=process.env.OUT||path.join(here,'frames');fs.mkdirSync(out,{recursive:true});
const b=await puppeteer.launch({executablePath:exe,headless:true});
const pg=await b.newPage();await pg.setViewport({width:1280,height:720,deviceScaleFactor:1.5});
await pg.goto('file://'+path.join(here,name+'.html')+'?static');
const D=await pg.evaluate(()=>window.DUR);
if(mode==='preview'){for(const t of ts){await pg.evaluate(t=>window.setT(t),+t);await pg.screenshot({path:`${out}/${name}_t${t}.png`})}}
else{const fps=30,n=Math.round(D*fps),dir=`${out}/${name}`;fs.rmSync(dir,{recursive:true,force:true});fs.mkdirSync(dir,{recursive:true});
 for(let i=0;i<n;i++){await pg.evaluate(t=>window.setT(t),i/fps);await pg.screenshot({path:`${dir}/f${String(i).padStart(4,'0')}.png`})}
 execSync(`ffmpeg -y -loglevel error -framerate ${fps} -i ${dir}/f%04d.png -c:v libx264 -pix_fmt yuv420p -crf 14 -vf "scale=1920:1080:flags=lanczos" ${here}/${name}.mp4`);
 execSync(`ffmpeg -y -loglevel error -framerate ${fps} -i ${dir}/f%04d.png -vf "fps=20,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" ${here}/${name}.gif`);
 fs.rmSync(dir,{recursive:true,force:true})}
await b.close();
