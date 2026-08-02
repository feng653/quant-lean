/**
 * Kimi K3 design tokens — "研究审计台" (research audit console).
 *
 * Principles:
 * - Warm neutral "paper & ink" base; one restrained teal accent.
 * - Status colors are always paired with icons and text labels; color is
 *   never the sole carrier of meaning (WCAG + fail-closed honesty).
 * - No decorative gradients, no neon, no oversized pill radii.
 * - `rise`/`fall` follow the A-share convention (red up / green down) and are
 *   reserved for signed market numbers — never for success/failure states.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: [
    "./index.html",
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        paper: '#f5f4f0',
        surface: '#ffffff',
        ink: {
          50: '#faf9f8',
          100: '#f3f2ef',
          200: '#e6e4e0',
          300: '#d3d0ca',
          400: '#aba69e',
          500: '#7d7870',
          600: '#5c574f',
          700: '#46423c',
          800: '#2b2823',
          900: '#1d1a16',
          950: '#100e0b',
        },
        accent: {
          50: '#eff7f6',
          100: '#d8ecea',
          200: '#b3d9d6',
          300: '#85bfbb',
          400: '#539e9a',
          500: '#32827f',
          600: '#236866',
          700: '#1e5453',
          800: '#1b4443',
          900: '#183837',
          950: '#0b2423',
        },
        ok: {
          fg: '#1c6b3c',
          bg: '#eff7f1',
          border: '#bcd9c4',
          strong: '#14532d',
        },
        warn: {
          fg: '#8a5a0b',
          bg: '#fdf6e4',
          border: '#ecd9a4',
          strong: '#713f12',
        },
        danger: {
          fg: '#a92c22',
          bg: '#fdefec',
          border: '#f2c7bf',
          strong: '#7f1d1d',
        },
        info: {
          fg: '#2b5ea7',
          bg: '#eef4fc',
          border: '#c3d7ef',
          strong: '#1e3f73',
        },
        rise: '#b23a2a',
        fall: '#1e7c5b',
      },
      fontFamily: {
        sans: [
          '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto',
          'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei',
          'Noto Sans CJK SC', 'sans-serif',
        ],
        mono: [
          'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas',
          'Liberation Mono', 'monospace',
        ],
      },
      fontSize: {
        '2xs': ['11px', { lineHeight: '14px' }],
        xs: ['12px', { lineHeight: '16px' }],
        sm: ['13px', { lineHeight: '19px' }],
        base: ['14px', { lineHeight: '21px' }],
        lg: ['16px', { lineHeight: '24px' }],
        xl: ['18px', { lineHeight: '26px' }],
        '2xl': ['22px', { lineHeight: '30px' }],
        '3xl': ['28px', { lineHeight: '36px' }],
      },
      borderRadius: {
        sm: '3px',
        DEFAULT: '4px',
        md: '6px',
        lg: '8px',
      },
      boxShadow: {
        overlay: '0 4px 16px rgba(29, 26, 22, 0.10), 0 1px 4px rgba(29, 26, 22, 0.08)',
        menu: '0 2px 10px rgba(29, 26, 22, 0.12)',
      },
      maxWidth: {
        content: '1440px',
      },
      minHeight: {
        touch: '40px',
      },
    },
  },
  plugins: [],
};
