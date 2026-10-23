const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();const node=()=>({value:'',checked:true,style:{},children:[],append(...x){this.children.push(...x)},replaceChildren(){this.children=[]},setAttribute(){}});
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,addEventListener(){}};
let confirms=0;const context=vm.createContext({document,window:{addEventListener(){}},console,confirm(){confirms++;return false},fetch:async()=>({ok:true,json:async()=>({connection:{message:'test'},drafts:[],settings:{}})})});
vm.runInContext(fs.readFileSync('index.html','utf8').split('<script>')[1].split('</script>')[0],context);
vm.runInContext(`
show({id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',place:'저장된 식당',photos:[],result:{titles:['제목'],body:'원문',tags:[]}});
if(hasDraftChanges())throw Error('저장본을 변경으로 오판');
$('new').onclick();if($('place').value)throw Error('저장본에서 새 글 이동 실패');
show({place:'생성된 식당',photos:[],result:{titles:['제목'],body:'생성문',tags:[]}},false);
if(!hasDraftChanges())throw Error('미저장 생성문 감지 실패');
$('new').onclick();if($('editor').value!=='생성문')throw Error('취소했는데 생성문 유실');
markDraftSaved();$('editor').value='수정';if(!hasDraftChanges())throw Error('편집 감지 실패');
$('editor').value='생성문';if(hasDraftChanges())throw Error('원상복구 변경 오판');
`,context);
assert.equal(confirms,1);console.log('PASS: saved→new without prompt, unsaved→cancel preserved, edit/revert detection');
