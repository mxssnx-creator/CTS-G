import {test} from 'node:test';import assert from 'node:assert/strict';import {createElement} from 'react';import {renderToStaticMarkup} from 'react-dom/server';import {VolumeRatioSliders} from './sliders.tsx';
test('slider controls render persisted values, bounded steps and one accessible label per independent scope',()=>{
 const html=renderToStaticMarkup(createElement(VolumeRatioSliders,{value:JSON.stringify({indicationTypes:{inverse:2},ranges:{micro:1.5}}),onChange:()=>{},overall:true,buckets:{indicationTypes:['Inverse','Tactic'],ranges:['Micro','Short']}}));
 assert.equal((html.match(/aria-label="Range · Micro volume ratio"/g)||[]).length,1);assert.match(html,/aria-label="Indication type · Inverse volume ratio"[^>]*value="2"/);assert.match(html,/aria-label="Range · Micro volume ratio"[^>]*value="1.5"/);assert.match(html,/min="0.1" max="5" step="0.1"/);assert.match(html,/Equalize all weights/);
});
