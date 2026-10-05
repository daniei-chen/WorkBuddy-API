import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const pluginRequire = createRequire(require.resolve('@next/eslint-plugin-next'));
const globRequire = createRequire(pluginRequire.resolve('fast-glob'));
const matchRequire = createRequire(globRequire.resolve('micromatch'));
const braces = matchRequire('braces');
assert.equal(matchRequire('braces/package.json').version, '3.0.3-workbuddy.1');
assert.deepEqual(braces.expand('a/{b,c}/d'), ['a/b/d','a/c/d']);
assert.deepEqual(braces.expand('{1..3}'), ['1','2','3']);
for (const method of ['parse','compile','expand','stringify']) {
  assert.throws(() => braces[method]('{'.repeat(12000) + 'a,b' + '}'.repeat(12000)), SyntaxError);
}
let ast = {type:'root', nodes:[]};
let cursor = ast;
for (let i=0; i<3000; i++) {
  const child = {type:'brace',nodes:[]}; cursor.nodes.push(child); cursor=child;
}
for (const method of ['compile','expand','stringify']) assert.throws(() => braces[method](ast), SyntaxError);
const cycle = {type:'root',nodes:[]}; cycle.nodes.push(cycle);
assert.throws(() => braces.compile(cycle), SyntaxError);
console.log('patched braces: patterns, deep nesting, direct AST and cycle regression passed');
