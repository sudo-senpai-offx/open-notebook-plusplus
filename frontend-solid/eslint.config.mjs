import parser from '@typescript-eslint/parser'
import solidPlugin from 'eslint-plugin-solid'

export default [
  {
    ignores: ['node_modules/', 'dist/', '.vite/'],
  },
  {
    files: ['**/*.{ts,tsx}'],
    plugins: {
      solid: solidPlugin,
    },
    languageOptions: {
      parser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      'no-unused-vars': 'off',
    },
  },
]
