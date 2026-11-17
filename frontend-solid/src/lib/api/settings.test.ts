import { describe, it, expect } from 'vitest'

describe('token counter', () => {
  it('counts approximate tokens (chars / 4)', () => {
    const countTokens = (text: string): number =>
      text ? Math.ceil(text.length / 4) : 0
    expect(countTokens('')).toBe(0)
    expect(countTokens('abcd')).toBe(1)
    expect(countTokens('abcde')).toBe(2)
    expect(countTokens('hello world')).toBe(3)
  })
})