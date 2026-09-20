// Ledgr tokens for React Native / Expo. Generated from tokens.json.
export const colors = {
  light: {
    'surface': '#f7f6f2',
    'surface-raised': '#ffffff',
    'surface-sunken': '#efede6',
    'border-subtle': '#e2ded2',
    'border': '#8a8578',
    'ink': '#1c211f',
    'ink-muted': '#565d58',
    'brand': '#6b4a2f',
    'brand-hover': '#553a24',
    'brand-tint': '#f1e6da',
    'on-brand': '#ffffff',
    'accent': '#e8b93a',
    'on-accent': '#2b1d0e',
    'panel': '#4a3220',
    'on-panel': '#ffffff',
    'on-panel-muted': '#eadfd2',
    'danger': '#a3312b',
    'danger-tint': '#f8e3e1',
    'warning': '#8a5a00',
    'warning-tint': '#fbf0d6',
    'info': '#1d6b7a',
    'info-tint': '#e0f0f3',
    'focus': '#6b4a2f',
  },
  dark: {
    'surface': '#161a18',
    'surface-raised': '#1f2421',
    'surface-sunken': '#111412',
    'border-subtle': '#2d332f',
    'border': '#6b736d',
    'ink': '#eceee9',
    'ink-muted': '#a3aaa4',
    'brand': '#d9b48f',
    'brand-hover': '#e6c8a8',
    'brand-tint': '#33281f',
    'on-brand': '#2b1d0e',
    'accent': '#e8b93a',
    'on-accent': '#161a18',
    'panel': '#33281f',
    'on-panel': '#eceee9',
    'on-panel-muted': '#d8c8b6',
    'danger': '#f08a80',
    'danger-tint': '#3d1f1c',
    'warning': '#e0b04a',
    'warning-tint': '#3a2e12',
    'info': '#6cc4d4',
    'info-tint': '#14303a',
    'focus': '#d9b48f',
  },
} as const;

export const spacing = { 'space-1': 4, 'space-2': 8, 'space-3': 12, 'space-4': 16, 'space-5': 24, 'space-6': 32, 'space-7': 48, 'space-8': 64 } as const;
export const radius = { 'radius-sm': 4, 'radius-md': 8, 'radius-lg': 12, 'radius-pill': 999 } as const;

export const fonts = {
  serif: 'Newsreader',
  sans: 'HankenGrotesk',
  mono: 'IBMPlexMono',
} as const;

export const type = {
  'display-lg': { fontFamily: fonts.serif, fontSize: 48, lineHeight: 52, fontWeight: '500' },
  'display-md': { fontFamily: fonts.serif, fontSize: 32, lineHeight: 38, fontWeight: '500' },
  'heading-lg': { fontFamily: fonts.sans, fontSize: 22, lineHeight: 30, fontWeight: '600' },
  'heading-md': { fontFamily: fonts.sans, fontSize: 17, lineHeight: 24, fontWeight: '600' },
  'body': { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, fontWeight: '400' },
  'body-sm': { fontFamily: fonts.sans, fontSize: 13, lineHeight: 20, fontWeight: '400' },
  'label': { fontFamily: fonts.sans, fontSize: 13, lineHeight: 16, fontWeight: '500' },
  'caption': { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, fontWeight: '400' },
  'figure-lg': { fontFamily: fonts.mono, fontSize: 28, lineHeight: 34, fontWeight: '500' },
  'figure': { fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, fontWeight: '400' },
} as const;

export type ThemeName = keyof typeof colors;
