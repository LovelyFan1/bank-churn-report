import test from 'node:test'
import assert from 'node:assert/strict'
import { createDialogStack } from '../src/utils/dialogStack.js'
test('nested dialogs retain background lock until last close and only top handles keys', () => {
  const stack=createDialogStack();let locked=0,restored=0
  const lock=()=>{locked++;return ()=>restored++}
  stack.acquire('form',lock);stack.acquire('idle',lock)
  assert.equal(locked,1);assert.equal(stack.isTop('form'),false);assert.equal(stack.isTop('idle'),true)
  assert.equal(stack.release('form'),false);assert.equal(restored,0)
  assert.equal(stack.release('idle'),true);assert.equal(restored,1)
  stack.release('idle');assert.equal(restored,1)
})
