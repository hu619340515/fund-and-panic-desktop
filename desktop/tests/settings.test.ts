import { expect, it } from 'vitest'
import { validateSettingsPatch } from '../src/main/settings'

it('设置先整体验证，拒绝混合非法参数与字符串开关', () => {
  const patch = {autoRefresh:false,windowOpacity:0.2}
  expect(() => validateSettingsPatch(patch)).toThrow()
  expect(patch).toEqual({autoRefresh:false,windowOpacity:0.2})
  expect(() => validateSettingsPatch({launchAtLogin:'true'})).toThrow()
  expect(() => validateSettingsPatch({refreshIntervalSeconds:'60'})).toThrow()
  expect(() => validateSettingsPatch({unknown:true})).toThrow()
  expect(validateSettingsPatch({autoRefresh:false,windowOpacity:0.8})).toEqual({autoRefresh:false,windowOpacity:0.8})
})
