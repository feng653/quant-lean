import { describe, expect, it } from 'vitest';

const sourceFiles = import.meta.glob(
  './**/*.{ts,tsx,css}',
  {
    eager: true,
    import: 'default',
    query: '?raw',
  },
) as Record<string, string>;

const emojiPattern = new RegExp(
  [
    '[\\u{1F1E6}-\\u{1F1FF}]',
    '[\\u{1F300}-\\u{1FAFF}]',
    '[\\u{2600}-\\u{27BF}]',
    '[\\u{2300}-\\u{23FF}]',
    '[\\u{2B00}-\\u{2BFF}]',
    '[\\u{FE0F}\\u{200D}]',
  ].join('|'),
  'u',
);

describe('frontend source character policy', () => {
  it('does not contain emoji or emoji presentation controls', () => {
    const offenders = Object.entries(sourceFiles)
      .filter(([, source]) => emojiPattern.test(source))
      .map(([path]) => path)
      .sort();

    expect(offenders).toEqual([]);
  });
});
