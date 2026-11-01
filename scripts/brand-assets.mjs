/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mkdirSync, writeFileSync, copyFileSync, readFileSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';

// Import authorized private sources externally; distribute only generated artwork.
const { values } = parseArgs({options:{kit:{type:'string'},out:{type:'string'},website:{type:'string'},bridge:{type:'string'},'bridge-navigation-only':{type:'boolean'}}});
const navigationOnly=values['bridge-navigation-only'];
if(!values.kit || (navigationOnly ? !values.bridge : !values.out || !values.website)) throw Error('Usage: node scripts/brand-assets.mjs --kit <private-kit> --website <website-checkout> --out <forge> [--bridge <private-bridge>]; or --kit <private-kit> --bridge <bridge> --bridge-navigation-only');
const root=values.out?resolve(values.out):null,kit=resolve(values.kit),website=values.website?resolve(values.website):null,bridge=values.bridge?resolve(values.bridge):null;
const hash=contents=>createHash('sha256').update(contents).digest('hex');
const pinned={'scripts/logo.mjs':'0ab3aa2a78b48307bac75dc7faf7e12f84eb2d44c538b9a4740be9d18aec6f95','scripts/lib/type.mjs':'7e3aff8953a9ec863c6a7aff9870d440f640e3e9608d824ec5862059d26ba2e8'};
for(const [file,expected] of Object.entries(pinned)) if(hash(readFileSync(join(kit,file)))!==expected) throw Error('Brand source differs from pinned source: '+file);
process.chdir(kit);
const {geometry,svg,placed,icon}=await import(pathToFileURL(join(kit,'scripts/logo.mjs')));
function writeBridgeNavigation(bridge){
 const inventoryPath=join(bridge,'assets/brand/inventory.json'),inventory=JSON.parse(readFileSync(inventoryPath));
 if(inventory.source!=='firefly-oss/brand-kit' || inventory.revision!=='3fc4834068fc531dad6fcff4719af2bb5b6d5c9e') throw Error('Bridge artwork differs from pinned brand source');
 const [left,top,parentWidth]=geometry().compact;
 for(const [theme,ink] of [['dark','#f3f1eb'],['light','#10110f']]){
  const source=`assets/brand/bridge-lockup-${theme}.svg`,entry=inventory.files.find(item=>item.path===source),original=readFileSync(join(bridge,source),'utf8');
  if(!entry || hash(original)!==entry.sha256) throw Error('Bridge source inventory mismatch: '+source);
  const parent=solid=>placed(svg({ink,slogan:false,solid,title:'',id:'bridge-parent-trail'}),left,top,parentWidth);
  const fullParent=parent(false);
  if(original.split(fullParent).length!==2) throw Error('Expected exactly one canonical full parent in '+source);
  const contents=original.replace(fullParent,parent(true)),path=`assets/brand/bridge-lockup-small-${theme}.svg`;
  writeFileSync(join(bridge,path),contents);
  inventory.files=inventory.files.filter(item=>item.path!==path);
  inventory.files.push({...entry,path,source,sha256:hash(contents)});
 }
 inventory.navigation={variant:'small-solid-parent',minimumParentWidthPx:48,generator:'External public Forge scripts/brand-assets.mjs --bridge-navigation-only'};
 writeFileSync(inventoryPath,JSON.stringify(inventory,null,2)+'\n');
 console.log('Generated two Bridge navigation lockups; existing product artwork and full-size assets preserved.');
}
if(navigationOnly){writeBridgeNavigation(bridge);process.exit(0);}
const websiteRevision=execFileSync('git',['rev-parse','HEAD'],{cwd:website,encoding:'utf8'}).trim();
if(websiteRevision!=='6a2d519ab917fb183e607bcc5b4ab13f3819748f') throw Error('Website source differs from pinned 6a2d519 factory source');
const {line,measure}=await import(pathToFileURL(join(kit,'scripts/lib/type.mjs')));
const require=createRequire(join(kit,'package.json'));
const sharp=(await import(pathToFileURL(require.resolve('sharp')))).default;
const {build}=await import(pathToFileURL(createRequire(join(root,'studio/package.json')).resolve('esbuild')));
const bundled=await build({stdin:{contents:`export {drawing} from ${JSON.stringify(join(website,'src/scripts/factory-drawing.ts'))}; export {SCENARIOS} from ${JSON.stringify(join(website,'src/scripts/factory-lines.ts'))};`,resolveDir:website,loader:'ts'},bundle:true,format:'esm',platform:'node',write:false,logLevel:'silent'});
const {drawing,SCENARIOS}=await import('data:text/javascript;base64,'+Buffer.from(bundled.outputFiles[0].text).toString('base64'));
const factory=drawing(SCENARIOS.find(item=>item.key==='onboarding').line);
const colors={dark:{ink:'#f3f1eb',bg:'#10110f',accent:'#ffb34a',muted:'#bfb8ab',line:'#474a42'},light:{ink:'#10110f',bg:'#f3f1eb',accent:'#ffb34a',muted:'#62645b',line:'#d8d4ca'}};
const parentOwner='Firefly Software Solutions Inc. and Firefly Software Foundation';
const proprietary='Proprietary artwork; product redistribution only';
const notice=(product='Forge')=>`<!-- Firefly marks and factory graphic systems are proprietary artwork of Firefly Software Solutions Inc., used with permission. ${product} is a trademark of the Firefly Software Foundation. Not Apache-2.0; see NOTICE. Generated by scripts/brand-assets.mjs; do not edit. -->\n`;
const originalNotice=(product)=>`<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0. ${product} is a trademark; no trademark rights granted. Generated by scripts/brand-assets.mjs. -->\n`;
const accessible=(body,title,desc,rights=notice())=>rights+body.replace('<svg ','<svg role="img" aria-labelledby="title desc" ').replace(/(<svg[^>]*>)/,`$1<title id="title">${title}</title><desc id="desc">${desc}</desc>`);
const fBody='M30 0V-84H4V-110H30V-122Q30 -148 56 -148H100L74-122H56V-110H84L58-84H56V0Z';
const fHot='M56-148H100L74-122H56Z';
function forgeGlyph(ink,accent,x=0,y=0,scale=1,small=false){return `<g transform="translate(${x} ${y}) scale(${scale})"><path d="${fBody}" fill="${ink}"/>${small?'':`<path d="${fHot}" fill="${accent}"/>`}</g>`;}
function forgeWord(theme,small=false){const c=colors[theme],size=200,x=64;return {width:x+measure('orge',{size,weight:500,tracking:-.045}),body:`<g transform="scale(.74 1)">${forgeGlyph(c.ink,c.accent,0,0,1)}</g>`+line('orge',{x,y:0,size,weight:500,tracking:-.045,fill:c.ink})};}
const [left,top,parentWidth,parentHeight]=geometry().compact,X=108;
function lockup(theme,small=false){const c=colors[theme],word=forgeWord(theme,small),separator=left+parentWidth+X,y=-54-parentHeight*.625;return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${left} ${y} ${parentWidth+X*2+word.width} ${parentHeight*1.25}">${placed(svg({ink:c.ink,slogan:false,solid:small,title:'',id:'forge-parent-trail'}),left,top,parentWidth)}<path d="M${separator} ${y}v${parentHeight*1.25}" stroke="${c.ink}" stroke-opacity=".55" stroke-width="4.32"/><g transform="translate(${separator+X} 0)">${word.body}</g></svg>`;}
function mark(theme,small=false,background=false){const c=colors[theme];return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">${background?`<rect width="192" height="192" rx="36" fill="${c.bg}"/>`:''}${forgeGlyph(c.ink,c.accent,45,165,.94,small)}</svg>`;}
const lightPaint={'#343b2d':'#d8d4ca','#22261d':'#bfb8ab','#1c1f18':'#a6a99b','#262a20':'#bfb8ab','#1b1e17':'#a3a091','#171914':'#858b7b','#24271f':'#62645b','#1a1c16':'#474a42','#15170f':'#34372e','#4a5240':'#eae7df','#353b2e':'#bfb8ab','#2b3025':'#a3a091','#4a5042':'#d8d4ca','#3b4034':'#bfb8ab','#30352b':'#9a9f8b','#40311b':'#ffb34a','#4b3a20':'#fcc271','#342918':'#cc8b33','#464a47':'#b4bdb8','#585d59':'#cdd2cc','#5f6460':'#8a908a','#e3e4de':'#62645b','#e6e7e1':'#474a42','#efe2cc':'#855414','#151713':'#ffffff','#ffd9a3':'#855414'};
function factorySvg(theme){let body=factory.body.replace(/<g class="marks">[\s\S]*<\/g>$/,'');if(theme==='light')body=body.replace(/#[a-fA-F0-9]{6}/g,color=>lightPaint[color.toLowerCase()]??color);return accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${factory.viewBox}">${body}</svg>`,'firefly forge factory','Conceptual Firefly factory illustration, adapted from the website onboarding composition. Original machine shapes, connected conveyors, office baseplates, side lane and API/event backbone geometry are preserved. Colors match the '+theme+' theme. This illustration is not a live runtime graph.');}
const manifest={version:2,source:'firefly-oss/brand-kit',revision:'3fc4834068fc531dad6fcff4719af2bb5b6d5c9e',website:{repository:'firefly-oss/firefly-software-website',revision:websiteRevision,scenario:'onboarding',renderer:'src/scripts/factory-drawing.ts',sourceFiles:Object.fromEntries(['src/scripts/factory-machines.ts','src/scripts/factory-drawing.ts','src/scripts/factory-lines.ts','src/scripts/factory-backbone.ts','src/scripts/factory-timeline.ts','scripts/brand/system/factory.mjs','scripts/brand/system/pieces.json','scripts/brand/system/tokens.json'].map(file=>[file,hash(readFileSync(join(website,file)))]))},generator:'scripts/brand-assets.mjs',identity:{forge:'Forged lowercase f: 148-unit ascender, 26-unit icon billets, 45-degree cut arms; wordmark stem optically normalized to Manrope Medium with74% horizontal scale and64-unit f advance; amber hot edge matching the Firefly chevron in both themes',bridge:'Architectural span: amber deck, two piers, open arch and terminal nodes'},files:[]};
function record(path,contents,owner,license,source){manifest.files.push({path,owner,license,...(source?{source}:{}),sha256:hash(contents)});}
function writeAsset(name,contents,owner=parentOwner,license=proprietary){mkdirSync(join(root,name,'..'),{recursive:true});writeFileSync(join(root,name),contents);record(name,contents,owner,license);}
function copyAsset(target,source){mkdirSync(join(root,target,'..'),{recursive:true});copyFileSync(join(root,source),join(root,target));const origin=manifest.files.find(item=>item.path===source);record(target,readFileSync(join(root,target)),origin.owner,origin.license,source);}
for(const theme of ['dark','light']){
 writeAsset(`assets/brand/forge-lockup-${theme}.svg`,accessible(lockup(theme),'firefly forge','The original Firefly artwork, hairline divider and a Forge wordmark whose first letter is the original forged F, in '+theme+' colors.'));
 writeAsset(`assets/brand/forge-lockup-small-${theme}.svg`,accessible(lockup(theme,true),'firefly forge','Small Firefly Forge lockup, with solid parent chevron and an amber-accented forged F.'));
 const word=forgeWord(theme);
 writeAsset(`assets/brand/forge-wordmark-${theme}.svg`,accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 -160 ${word.width+8} 210">${word.body}</svg>`,'firefly forge','The Forge wordmark with a cut-billet F and outlined Manrope Medium orge.',originalNotice('Forge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
 writeAsset(`assets/brand/forge-mark-${theme}.svg`,accessible(mark(theme),'firefly forge','The standalone forged F: a solid stem, two cut billet arms and one hot edge.',originalNotice('Forge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
 writeAsset(`assets/brand/forge-mark-mono-${theme}.svg`,accessible(mark(theme,true),'firefly forge','The one-color forged F for small placements.',originalNotice('Forge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
 writeAsset(`assets/brand/forge-favicon-${theme}.svg`,accessible(mark(theme,true,true),'firefly forge','The one-color forged F on its '+theme+' application tile.',originalNotice('Forge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
 writeAsset(`assets/factory/forge-factory-${theme}.svg`,factorySvg(theme),'Firefly Software Solutions Inc.',proprietary);
 const c=colors[theme],scene=factorySvg(theme).replace(/^<!--[\s\S]*?-->\n/,'').replace(/<title[\s\S]*?<\/desc>/,'');
 const banner=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 440"><rect width="1440" height="440" fill="${c.bg}"/>${placed(lockup(theme),56,54,520)}${line('From brief to',{x:56,y:280,size:46,weight:400,fill:c.ink})}${line('verified software.',{x:56,y:340,size:46,weight:400,fill:c.ink})}${placed(scene,620,72,790)}</svg>`;
 writeAsset(`assets/banner-${theme}.svg`,accessible(banner,'firefly forge — From brief to verified software','Forge identity beside the original website factory geometry, in '+theme+' colors.'));
 for(const folder of ['studio/public/assets','docs/assets'])for(const [target,source] of [[`forge-lockup-${theme}.svg`,`assets/brand/forge-lockup-${theme}.svg`],[`forge-mark-${theme}.svg`,`assets/brand/forge-mark-${theme}.svg`],[`forge-mark-mono-${theme}.svg`,`assets/brand/forge-mark-mono-${theme}.svg`],[`forge-factory-${theme}.svg`,`assets/factory/forge-factory-${theme}.svg`],[`forge-favicon-${theme}.svg`,`assets/brand/forge-favicon-${theme}.svg`],[`banner-${theme}.svg`,`assets/banner-${theme}.svg`]])copyAsset(`${folder}/${target}`,source);
}
for(const [target,source] of [['assets/brand/forge-lockup-small.svg','assets/brand/forge-lockup-small-dark.svg'],['assets/brand/forge-wordmark.svg','assets/brand/forge-wordmark-dark.svg'],['assets/banner.svg','assets/banner-dark.svg'],['studio/public/assets/forge-lockup.svg','assets/brand/forge-lockup-small-dark.svg'],['studio/public/favicon.svg','assets/brand/forge-favicon-dark.svg'],['docs/assets/forge-lockup.svg','assets/brand/forge-lockup-small-dark.svg'],['docs/assets/favicon.svg','assets/brand/forge-favicon-dark.svg'],['docs/assets/banner.svg','assets/banner-dark.svg']])copyAsset(target,source);
writeAsset('assets/brand/firefly-icon.svg',accessible(icon({solid:true}),'Firefly','The unmodified original Firefly spark and chevron icon.'));
for(const name of ['banner','banner-dark','banner-light','brand/forge-lockup-dark','brand/forge-lockup-light','brand/forge-mark-dark','brand/forge-mark-light']){
 const png=join(root,'assets',name+'.png');await sharp(Buffer.from(readFileSync(join(root,'assets',name+'.svg')))).resize(name.startsWith('banner')?1440:name.includes('mark-')?512:1200).png().toFile(png);const source=manifest.files.find(item=>item.path==='assets/'+name+'.svg');record('assets/'+name+'.png',readFileSync(png),source.owner,source.license,'assets/'+name+'.svg');
}
const publicNotice=`Firefly Forge visual assets\n\nFirefly parent marks and the website factory graphic systems are proprietary to Firefly Software Solutions Inc. Copyright 2026. All rights reserved. Used with permission by the Firefly Software Foundation; not Apache-2.0. They may accompany unmodified copies of this product; no general mark or graphic-system rights are granted.\n\nThe original Forged F emblem, Forge wordmark and name are Firefly Software Foundation trademarks. First-party F artwork is Apache-2.0 as a copyright work, with no trademark rights granted. Combined lockups and banners include proprietary Firefly artwork.\n\nParent source: firefly-oss/brand-kit 2.0.0 commit3fc4834068fc531dad6fcff4719af2bb5b6d5c9e. Factory source: firefly-oss/firefly-software-website commit${websiteRevision}, onboarding scenario, src/scripts/factory-drawing.ts with original conveyors, machines and backbone geometry. Only generated assets are distributed; private source/schema modules stay external. Light versions remap material colors; the original geometry is preserved. Conceptual illustration, not a runtime graph. inventory.json records source hashes and generated asset integrity.\n\nOperational factory: generated geometry, materials and mechanical keyframes from the pinned website factory-machines.ts builders. The application supplies recorded stage states; mechanical cycles do not estimate completion. Generated 3D assets retain proprietary Firefly graphic-system rights. Three.js Copyright 2010–2026 three.js authors, MIT License; retain public/licenses/three-LICENSE.txt.\n\nManrope: Copyright 2019 The Manrope Project Authors, SIL Open Font License1.1. Keep OFL.txt with fonts. Lucide icons retain the ISC/MIT notices bundled in public/licenses.\n`;
for(const target of ['assets/brand/NOTICE','studio/public/NOTICE.txt','docs/assets/NOTICE.txt'])writeAsset(target,publicNotice,parentOwner,'Notice; marks reserved');
for(const name of ['assets/fonts/manrope/manrope-latin-wght-normal.woff2','assets/fonts/manrope/OFL.txt','studio/public/fonts/manrope-latin-wght-normal.woff2','studio/public/fonts/OFL.txt','docs/assets/fonts/manrope-latin-wght-normal.woff2','docs/assets/fonts/OFL.txt'])record(name,readFileSync(join(root,name)),'The Manrope Project Authors','OFL-1.1','@fontsource-variable/manrope5.3.0; Firefly Weave font snapshot');
record('studio/public/licenses/lucide-LICENSE.txt',readFileSync(join(root,'studio/public/licenses/lucide-LICENSE.txt')),'Lucide and Feather contributors','ISC and MIT');
for(const name of ['forge-operational-model.json','forge-operational-model.json.gz','operational-model-provenance.json'])if(existsSync(join(root,'assets/factory',name))){record('assets/factory/'+name,readFileSync(join(root,'assets/factory',name)),'Firefly Software Solutions Inc.',proprietary);copyAsset('studio/public/assets/'+name,'assets/factory/'+name);}
if(existsSync(join(root,'assets/factory/operational-model-provenance.json'))){manifest.operationalFactory={provenance:'assets/factory/operational-model-provenance.json',renderer:'studio/src/FactoryScene.tsx',motion:'Mechanical animation runs only for recorded active stages; output travels only after an observed stage-state receipt. No estimated production progress.'};const threeLicense=readFileSync(join(root,'studio/node_modules/three/LICENSE'));writeAsset('studio/public/licenses/three-LICENSE.txt',threeLicense,'three.js authors','MIT');}
writeFileSync(join(root,'assets/brand/inventory.json'),JSON.stringify(manifest,null,2)+'\n');

if(bridge){
 const bridgeFiles=[];
 const bGlyph=(c,mono=false)=>`<path d="M12-88H136V0H110V-44C110-82 38-82 38-44V0H12Z" fill="${c.ink}"/><path d="M0-112H148V-88H0Z" fill="${mono?c.ink:c.accent}"/><rect x="0" y="-124" width="12" height="12" rx="3" fill="${c.ink}"/><rect x="136" y="-124" width="12" height="12" rx="3" fill="${c.ink}"/>`;
 const bridgeWrite=(name,contents,owner=parentOwner,license=proprietary)=>{mkdirSync(join(bridge,name,'..'),{recursive:true});writeFileSync(join(bridge,name),contents);bridgeFiles.push({path:name,owner,license,sha256:hash(contents)});};
 for(const theme of ['dark','light']){
  const c=colors[theme],nameWidth=measure('agent bridge',{size:166,weight:500,tracking:-.035}),separator=left+parentWidth+X,y=-54-parentHeight*.625,productX=separator+X;
  const bridgeWord=`<g transform="translate(${productX} 0)">${bGlyph(c)}</g>${line('agent bridge',{x:productX+178,y:-9,size:166,weight:500,tracking:-.035,fill:c.ink})}`;
  const bridgeLockup=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${left} ${y} ${parentWidth+X*2+178+nameWidth} ${parentHeight*1.25}">${placed(svg({ink:c.ink,slogan:false,solid:false,title:'',id:'bridge-parent-trail'}),left,top,parentWidth)}<path d="M${separator} ${y}v${parentHeight*1.25}" stroke="${c.ink}" stroke-opacity=".55" stroke-width="4.32"/>${bridgeWord}</svg>`;
  bridgeWrite(`assets/brand/bridge-lockup-${theme}.svg`,accessible(bridgeLockup,'Firefly Agent Bridge','Official Firefly parent logo beside the architectural span emblem and outlined agent bridge name.',notice('Agent Bridge')));
  bridgeWrite(`assets/brand/bridge-mark-${theme}.svg`,accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><g transform="translate(26 154) scale(.94)">${bGlyph(c)}</g></svg>`,'Agent Bridge','An open arch between two piers, joined by an amber deck and terminal nodes.',originalNotice('Agent Bridge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
  bridgeWrite(`assets/brand/bridge-mark-mono-${theme}.svg`,accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><g transform="translate(26 154) scale(.94)">${bGlyph(c,true)}</g></svg>`,'Agent Bridge','One-color architectural span for compact navigation.',originalNotice('Agent Bridge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
  bridgeWrite(`assets/brand/bridge-favicon-${theme}.svg`,accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192"><rect width="192" height="192" rx="36" fill="${c.bg}"/><g transform="translate(26 154) scale(.94)">${bGlyph(c,true)}</g></svg>`,'Agent Bridge','Architectural span on a '+theme+' application tile.',originalNotice('Agent Bridge')),'Firefly Software Foundation','Apache-2.0; trademark reserved');
  bridgeWrite(`assets/banner-${theme}.svg`,accessible(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 420"><rect width="1440" height="420" fill="${c.bg}"/>${placed(bridgeLockup,60,54,890)}${line('Local coding agents.',{x:60,y:291,size:44,fill:c.ink})}${line('Durable jobs and workflows.',{x:60,y:349,size:44,fill:c.ink})}<g transform="translate(1120 345) scale(1.6)">${bGlyph(c)}</g></svg>`,'Firefly Agent Bridge','Distinct internal bridge identity, in '+theme+' colors.',notice('Agent Bridge')));
  for(const name of [`brand/bridge-lockup-${theme}`,`banner-${theme}`]){const destination=join(bridge,'assets',name+'.png');await sharp(readFileSync(join(bridge,'assets',name+'.svg'))).resize(name.startsWith('banner')?1440:1800).png().toFile(destination);const source=bridgeFiles.find(item=>item.path==='assets/'+name+'.svg');bridgeFiles.push({path:'assets/'+name+'.png',owner:source.owner,license:source.license,sha256:hash(readFileSync(destination))});}
 }
 for(const [target,source] of [['assets/banner.png','assets/banner-dark.png'],['assets/banner.svg','assets/banner-dark.svg']]) { const contents=readFileSync(join(bridge,source)); const original=bridgeFiles.find(item=>item.path===source); bridgeWrite(target,contents,original.owner,original.license); }
 const bridgeLogo=await sharp(readFileSync(join(bridge,'assets/brand/bridge-favicon-dark.svg'))).resize(512).png().toBuffer();
 bridgeWrite('assets/logo.png',bridgeLogo,'Firefly Software Foundation','Apache-2.0; trademark reserved');
 const mono=readFileSync(join(bridge,'assets/brand/bridge-mark-mono-dark.svg'));
 bridgeWrite('assets/brand/bridge-mark.svg',mono,'Firefly Software Foundation','Apache-2.0; trademark reserved');
 const bridgeNotice=`Firefly Agent Bridge visual assets\n\nFirefly parent marks are proprietary to Firefly Software Solutions Inc. Copyright 2026. All rights reserved. Used with permission by the Firefly Software Foundation; not Apache-2.0. They may accompany unmodified copies of this product; no general mark rights are granted.\n\nThe original architectural span emblem and Agent Bridge name are Firefly Software Foundation trademarks. First-party span artwork is Apache-2.0 as a copyright work, with no trademark rights granted. Combined lockups and banners include proprietary Firefly parent artwork.\n\nParent source: firefly-oss/brand-kit 2.0.0 commit3fc4834068fc531dad6fcff4719af2bb5b6d5c9e. Only generated artwork is distributed; private brand-kit source remains external. inventory.json records generated asset integrity and individual ownership.\n\nManrope: Copyright 2019 The Manrope Project Authors, SIL Open Font License1.1. Keep OFL.txt with fonts.\n`;
 bridgeWrite('assets/brand/NOTICE',bridgeNotice,parentOwner,'Notice; marks reserved');
 for(const name of ['assets/fonts/manrope/manrope-latin-wght-normal.woff2','assets/fonts/manrope/OFL.txt'])if(existsSync(join(bridge,name)))bridgeFiles.push({path:name,owner:'The Manrope Project Authors',license:'OFL-1.1',sha256:hash(readFileSync(join(bridge,name)))});
 writeFileSync(join(bridge,'assets/brand/inventory.json'),JSON.stringify({version:2,source:manifest.source,revision:manifest.revision,identity:manifest.identity.bridge,generator:'External public Forge scripts/brand-assets.mjs',files:bridgeFiles},null,2)+'\n');
 writeBridgeNavigation(bridge);
}
console.log(`Generated Forge F, ${manifest.files.length} inventoried public assets, both factory themes${bridge?' and distinct private Bridge span artwork':''}.`);
