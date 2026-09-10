import {build} from 'esbuild';
import {compile} from '@vue/compiler-dom';
import {mkdir,copyFile,readFile,writeFile,readdir} from 'node:fs/promises';
import path from 'node:path';
await mkdir('dist', {recursive: true});
const templates=new Map();
const result=await build({entryPoints:['src/app.js'],bundle:true,minify:true,outfile:'dist/app.js',metafile:true,
  alias:{vue:'vue/dist/vue.runtime.esm-bundler.js'},define:{__VUE_OPTIONS_API__:'true',__VUE_PROD_DEVTOOLS__:'false',
  __VUE_PROD_HYDRATION_MISMATCH_DETAILS__:'false','process.env.NODE_ENV':'"production"'},
  target:['chrome109'],legalComments:'eof',plugins:[{name:'local-vue-templates',setup(plugin){
    plugin.onResolve({filter:/^cqg-template:/},args=>({path:args.path,namespace:'vue-template'}));
    plugin.onLoad({filter:/.*/,namespace:'vue-template'},args=>({contents:templates.get(args.path),loader:'js',resolveDir:process.cwd()}));
    plugin.onLoad({filter:/src[\\/](app|components)\.js$/},async args=>{
      const source=await readFile(args.path,'utf8'),imports=[];
      const contents=source.replace(/template:\s*`([\s\S]*?)`/g,(_match,template)=>{
        const name='__template'+templates.size,key='cqg-template:'+templates.size;
        templates.set(key,compile(template,{mode:'module',prefixIdentifiers:true,hoistStatic:true}).code);
        imports.push(`import {render as ${name}} from '${key}';`);
        return 'render:'+name;
      });
      return {contents:imports.join('\n')+'\n'+contents,loader:'js',resolveDir:path.dirname(args.path)};
    });
  }}]});
await copyFile('index.html','dist/index.html');
await copyFile('src/style.css','dist/style.css');
await mkdir('dist/avatars', {recursive:true});
for(const file of await readdir('assets/avatars'))await copyFile('assets/avatars/'+file,'dist/avatars/'+file);
const packages=new Set(Object.keys(result.metafile.inputs).filter(file=>file.startsWith('node_modules/')).map(file=>{
  const parts=file.split('/');return parts[1].startsWith('@')?parts.slice(1,3).join('/'):parts[1];
}));
const licenses=[];
for(const name of [...packages].sort()){
  const folder='node_modules/'+name,metadata=JSON.parse(await readFile(folder+'/package.json','utf8'));
  const license=(await readdir(folder)).find(file=>/^license(?:\.txt|\.md)?$/i.test(file));
  if(!license)throw Error('Missing bundled dependency license: '+name);
  licenses.push(name+' '+metadata.version+'\n'+await readFile(folder+'/'+license,'utf8'));
}
await writeFile('dist/THIRD_PARTY_LICENSES.txt',licenses.join('\n\n'+'='.repeat(70)+'\n\n'),'utf8');
console.log('Offline frontend built: frontend/dist/index.html');
