/* Auto-Orchestrator V7.2 — original 12x20 pixel-art office avatars.
 *
 * Every pixel is drawn with fillRect. There are no images, fonts or external
 * assets. A deterministic variant (skin, hair, accessory) is derived from the
 * session key by the scene; the agent colour only becomes the shirt accent, so
 * identity is never invented from a display name.
 */
(() => {
  'use strict';

  const SPRITE_WIDTH = 12;
  const SPRITE_HEIGHT = 20;
  const HIT_WIDTH = 14;
  const HIT_HEIGHT = 24;

  const SKINS = [
    { base: '#f3c7a3', shade: '#d8a47c', dark: '#b27c55' },
    { base: '#e7b28a', shade: '#c88f68', dark: '#9f6847' },
    { base: '#cd9469', shade: '#ad7650', dark: '#845537' },
    { base: '#a56b46', shade: '#875334', dark: '#613a24' },
    { base: '#7a4d33', shade: '#623c28', dark: '#422719' },
    { base: '#f7ddc6', shade: '#dfbd9f', dark: '#bd977a' },
  ];
  const HAIRS = ['#241d1a', '#4b3323', '#80542f', '#d7bd86', '#3b3654', '#a84a38', '#6a7280', '#e2e6ea'];
  const ACCESSORIES = ['none', 'glasses', 'headphones', 'cap', 'bun', 'beard', 'earring'];
  const SEATED = new Set([
    'working', 'reading', 'testing', 'reviewing', 'awaiting', 'blocked',
    'completed', 'failed', 'unknown',
  ]);
  const BADGE = {
    completed: '#70e0a0',
    failed: '#f47d7d',
    blocked: '#f2b45f',
    unknown: '#9aa0aa',
    awaiting: '#f2b45f',
    warning: '#f2b45f',
  };
  const INK = '#1a202b';
  const DEEP = '#0b0f16';
  const FALLBACK_ACCENT = '#7dd3fc';

  function hash(value) {
    const input = String(value == null ? '' : value);
    let result = 0x811c9dc5;
    for (let index = 0; index < input.length; index += 1) {
      result ^= input.charCodeAt(index);
      result = Math.imul(result, 0x01000193);
    }
    return result >>> 0;
  }

  function variantFor(seed) {
    const value = hash(seed);
    return {
      skin: value % SKINS.length,
      hair: (value >>> 3) % HAIRS.length,
      accessory: ACCESSORIES[(value >>> 6) % ACCESSORIES.length],
      hairStyle: (value >>> 10) % 5,
      seed: String(seed == null ? '' : seed),
    };
  }

  function parseHex(value) {
    if (typeof value !== 'string') return null;
    const raw = value.trim();
    if (/^#[0-9a-f]{3}$/i.test(raw)) {
      return {
        r: parseInt(raw[1] + raw[1], 16),
        g: parseInt(raw[2] + raw[2], 16),
        b: parseInt(raw[3] + raw[3], 16),
      };
    }
    if (/^#[0-9a-f]{6}$/i.test(raw)) {
      return {
        r: parseInt(raw.slice(1, 3), 16),
        g: parseInt(raw.slice(3, 5), 16),
        b: parseInt(raw.slice(5, 7), 16),
      };
    }
    return null;
  }

  function toHex(rgb) {
    const clamp = (value) => Math.max(0, Math.min(255, Math.round(value)));
    return `#${clamp(rgb.r).toString(16).padStart(2, '0')}${clamp(rgb.g).toString(16).padStart(2, '0')}${clamp(rgb.b).toString(16).padStart(2, '0')}`;
  }

  function mix(a, b, amount) {
    const first = parseHex(a) || parseHex(FALLBACK_ACCENT);
    const second = parseHex(b) || parseHex('#000000');
    const t = Math.max(0, Math.min(1, amount));
    return toHex({
      r: first.r + (second.r - first.r) * t,
      g: first.g + (second.g - first.g) * t,
      b: first.b + (second.b - first.b) * t,
    });
  }

  function safeColor(value, fallback = FALLBACK_ACCENT) {
    return parseHex(value) ? value.trim() : fallback;
  }

  function resolveVariant(options) {
    const skin = options && options.skin;
    const base = skin && typeof skin === 'object'
      ? skin
      : variantFor(skin == null ? options && options.key : skin);
    const skinIndex = Number.isInteger(base.skin)
      ? ((base.skin % SKINS.length) + SKINS.length) % SKINS.length
      : (Number.isInteger(skin) ? ((skin % SKINS.length) + SKINS.length) % SKINS.length : 0);
    const hairIndex = Number.isInteger(base.hair)
      ? ((base.hair % HAIRS.length) + HAIRS.length) % HAIRS.length
      : 0;
    const hairStyle = Number.isInteger(base.hairStyle)
      ? ((base.hairStyle % 5) + 5) % 5
      : 0;
    const accessory = ACCESSORIES.includes(base.accessory) ? base.accessory : 'none';
    return {
      skin: skinIndex,
      hair: hairIndex,
      hairStyle,
      accessory,
      colors: SKINS[skinIndex],
      hairColor: HAIRS[hairIndex],
    };
  }

  function draw(ctx, options = {}) {
    if (!ctx || typeof ctx.fillRect !== 'function') return;
    const scale = Number.isFinite(options.scale) && options.scale > 0 ? options.scale : 1;
    const x = Number.isFinite(options.x) ? options.x : 0;
    const y = Number.isFinite(options.y) ? options.y : 0;
    const state = typeof options.state === 'string' ? options.state : 'idle';
    const frame = Math.abs(Math.floor(Number(options.frame) || 0));
    const phase = frame % 4;
    const facing = ['up', 'down', 'left', 'right'].includes(options.facing) ? options.facing : 'down';
    const label = typeof options.label === 'string' ? options.label.trim() : '';
    const warning = options.warning === true || options.warning === 'start-only';
    const accent = safeColor(options.accent, FALLBACK_ACCENT);
    const accentDark = mix(accent, '#06080c', 0.48);
    const accentLight = mix(accent, '#ffffff', 0.34);
    const pants = mix(accent, '#141922', 0.74);
    const variant = resolveVariant(options);
    const skin = variant.colors;
    const hair = variant.hairColor;
    const seated = ['walking', 'idle', 'spawn'].includes(state) ? false
      : options.seated === false ? false
        : options.seated === true ? true : SEATED.has(state);
    const walking = state === 'walking';
    const bob = !seated && (walking ? (phase % 2 === 1 ? -1 : 0) : (state === 'idle' && phase === 1 ? -1 : 0));

    ctx.save();
    ctx.translate(x, y + bob);
    ctx.scale(scale, scale);
    if (facing === 'left') ctx.scale(-1, 1);

    const pixel = (px, py, width, height, color) => {
      if (width <= 0 || height <= 0) return;
      ctx.fillStyle = color;
      ctx.fillRect(px - 6, py - SPRITE_HEIGHT, width, height);
    };

    /* shadow (pixel block, not a soft ellipse) */
    pixel(2, 20, 8, 1, 'rgba(4,7,11,0.42)');
    pixel(3, 21, 6, 1, 'rgba(4,7,11,0.24)');

    if (seated) {
      drawSeated({
        pixel, state, phase, facing, skin, hair, accent, accentDark, accentLight,
        pants, variant, warning,
      });
    } else {
      drawStanding({
        pixel, state, phase, walking, facing, skin, hair, accent, accentDark,
        accentLight, pants, variant, warning,
      });
    }

    if (state === 'spawn') {
      const sparkle = phase % 2 === 0 ? '#f4d38a' : accentLight;
      pixel(9, 3, 1, 1, sparkle);
      pixel(1, 6, 1, 1, sparkle);
      pixel(10, 14, 1, 1, sparkle);
    }

    if (options.selected === true) {
      pixel(1, 21, 2, 1, accentLight);
      pixel(9, 21, 2, 1, accentLight);
    }

    if (label) {
      /* Undo the horizontal flip so a left-facing label stays readable. */
      if (facing === 'left') ctx.scale(-1, 1);
      ctx.save();
      ctx.font = '4px ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'alphabetic';
      const width = Math.max(8, Math.min(34, ctx.measureText(label.slice(0, 18)).width + 3));
      ctx.fillStyle = 'rgba(6,9,14,0.82)';
      ctx.fillRect(-width / 2, -28, width, 6);
      ctx.fillStyle = '#dbe4ef';
      ctx.fillText(label.slice(0, 18), 0, -23.5);
      ctx.restore();
    }

    ctx.restore();
  }

  function drawStanding(parts) {
    const { pixel, state, phase, walking, facing, skin, hair, accent, accentDark, accentLight, pants, variant, warning } = parts;
    const side = facing === 'left' || facing === 'right';
    const back = facing === 'up';
    const leftLeg = walking ? (phase === 1 ? 3 : phase === 3 ? 5 : 4) : 4;
    const rightLeg = walking ? (phase === 1 ? 7 : phase === 3 ? 5 : 6) : 6;
    const armSwing = walking && phase === 1 ? 1 : walking && phase === 3 ? -1 : 0;

    /* legs and shoes */
    pixel(leftLeg, 14, 2, 4, pants);
    pixel(rightLeg, 14, 2, 4, pants);
    pixel(leftLeg - 1, 18, 3, 2, mix(pants, '#05070a', 0.52));
    pixel(rightLeg, 18, 3, 2, mix(pants, '#05070a', 0.52));

    /* torso */
    pixel(3, 8, 6, 7, accent);
    pixel(3, 8, 1, 7, accentDark);
    pixel(8, 8, 1, 7, mix(accent, '#ffffff', 0.12));
    pixel(4, 8, 4, 1, accentLight);
    pixel(3, 13, 6, 1, mix(accent, '#080b10', 0.6));
    pixel(5, 11, 2, 1, accentLight);

    /* neck + head */
    pixel(5, 7, 2, 2, skin.shade);
    pixel(3, 2, 6, 6, skin.base);
    pixel(2, 4, 1, 2, skin.shade);
    pixel(9, 4, 1, 2, skin.shade);
    pixel(3, 7, 6, 1, skin.shade);

    /* arms */
    pixel(2, 9 + armSwing, 1, 5, accent);
    pixel(9, 9 - armSwing, 1, 5, accent);
    pixel(2, 12 + armSwing, 1, 2, skin.base);
    pixel(9, 12 - armSwing, 1, 2, skin.base);

    /* face */
    if (!back) {
      if (side) {
        pixel(7, 4, 1, 1, INK);
        pixel(8, 5, 1, 1, skin.shade);
        pixel(8, 6, 1, 1, skin.dark);
      } else {
        pixel(4, 4, 1, 1, INK);
        pixel(7, 4, 1, 1, INK);
        pixel(5, 6, 2, 1, skin.dark);
      }
    }

    drawHair(pixel, hair, variant.hairStyle, back);
    drawAccessory(pixel, variant.accessory, accent, accentDark, back);

    if (state === 'reading') {
      pixel(7, 11, 4, 3, accentDark);
      pixel(8, 11, 3, 2, '#eef3f8');
      pixel(9, 11, 1, 2, '#c7d2dc');
    } else if (state === 'testing') {
      pixel(8, 10, 3, 4, '#d7dee7');
      pixel(9, 12, 1, 1, BADGE.completed);
      pixel(10, 11, 1, 1, BADGE.completed);
    } else if (state === 'reviewing') {
      pixel(9, 10, 2, 1, accentLight);
      pixel(8, 11, 1, 2, accentLight);
      pixel(11, 11, 1, 2, accentLight);
      pixel(9, 13, 2, 1, accentLight);
      pixel(10, 14, 1, 1, accentDark);
    } else if (state === 'working') {
      pixel(7, 11, 4, 3, '#202936');
      pixel(8, 11, 2, 1, accent);
    }

    if (state === 'awaiting') drawBubble(pixel, phase);
    else if (state === 'blocked') drawWarningBadge(pixel);
    else if (state === 'completed') drawCheckBadge(pixel);
    else if (state === 'failed') drawCrossBadge(pixel);
    else if (state === 'unknown') drawQuestionBadge(pixel);

    if (warning) {
      pixel(10, 8, 2, 2, BADGE.warning);
      pixel(10, 8, 1, 1, '#fff6dd');
    }
  }

  function drawSeated(parts) {
    const { pixel, state, phase, facing, skin, hair, accent, accentDark, accentLight, pants, variant, warning } = parts;
    const side = facing === 'left' || facing === 'right';
    const back = facing === 'up';
    const lean = state === 'blocked' ? 1 : 0;

    /* lap and legs */
    pixel(3, 14 + lean, 6, 2, pants);
    if (side) {
      pixel(6, 15 + lean, 4, 2, pants);
      pixel(9, 18 + lean, 2, 2, mix(pants, '#05070a', 0.52));
      pixel(4, 18 + lean, 2, 2, mix(pants, '#05070a', 0.52));
    } else {
      pixel(4, 16 + lean, 2, 3, pants);
      pixel(6, 16 + lean, 2, 3, pants);
      pixel(3, 19 + lean, 3, 1, mix(pants, '#05070a', 0.52));
      pixel(6, 19 + lean, 3, 1, mix(pants, '#05070a', 0.52));
    }

    /* torso */
    pixel(3, 8 + lean, 6, 7, accent);
    pixel(3, 8 + lean, 1, 7, accentDark);
    pixel(8, 8 + lean, 1, 7, mix(accent, '#ffffff', 0.12));
    pixel(4, 8 + lean, 4, 1, accentLight);
    pixel(5, 11 + lean, 2, 1, accentLight);

    /* neck + head */
    pixel(5, 7 + lean, 2, 2, skin.shade);
    pixel(3, 2 + lean, 6, 6, skin.base);
    pixel(2, 4 + lean, 1, 2, skin.shade);
    pixel(9, 4 + lean, 1, 2, skin.shade);
    pixel(3, 7 + lean, 6, 1, skin.shade);

    /* face */
    if (!back) {
      if (side) {
        pixel(7, 4 + lean, 1, 1, INK);
        pixel(8, 5 + lean, 1, 1, skin.shade);
        pixel(8, 6 + lean, 1, 1, skin.dark);
      } else {
        pixel(4, 4 + lean, 1, 1, INK);
        pixel(7, 4 + lean, 1, 1, INK);
        pixel(5, 6 + lean, 2, 1, skin.dark);
      }
    }

    drawHair(pixel, hair, variant.hairStyle, back);
    drawAccessory(pixel, variant.accessory, accent, accentDark, back);

    /* arms are state-specific */
    if (state === 'working') {
      const tap = phase % 2 === 0 ? 0 : 1;
      pixel(2, 9 + lean, 2, 4, accent);
      pixel(3, 12 + lean, 2, 2, skin.base);
      pixel(8, 9 + lean, 2, 4, accent);
      pixel(7 + tap, 12 + lean, 2, 2, skin.base);
      pixel(3, 14 + lean, 2, 1, skin.base);
      pixel(7 + tap, 14 + lean, 2, 1, skin.base);
    } else if (state === 'reading') {
      /* open book */
      pixel(1, 12 + lean, 10, 4, accentDark);
      pixel(2, 12 + lean, 4, 3, '#e9eef4');
      pixel(6, 12 + lean, 4, 3, '#f4f7fa');
      pixel(5, 12 + lean, 2, 3, mix(accent, '#0a0e15', 0.35));
      pixel(4, 13 + lean, 1, 1, '#b7c2ce');
      pixel(7, 13 + lean, 1, 1, '#b7c2ce');
      pixel(2, 8 + lean, 1, 5, accent);
      pixel(9, 8 + lean, 1, 5, accent);
    } else if (state === 'testing') {
      /* clipboard with a green check */
      pixel(7, 10 + lean, 4, 6, '#d7dee7');
      pixel(7, 10 + lean, 4, 1, accentDark);
      pixel(8, 12 + lean, 2, 1, '#6b7784');
      pixel(8, 14 + lean, 1, 1, BADGE.completed);
      pixel(9, 13 + lean, 1, 1, BADGE.completed);
      pixel(10, 12 + lean, 1, 1, BADGE.completed);
      pixel(2, 9 + lean, 2, 4, accent);
      pixel(9, 9 + lean, 1, 3, accent);
      pixel(3, 12 + lean, 1, 2, skin.base);
    } else if (state === 'reviewing') {
      /* magnifier */
      pixel(8, 10 + lean, 3, 1, accentLight);
      pixel(7, 11 + lean, 1, 3, accentLight);
      pixel(11, 11 + lean, 1, 3, accentLight);
      pixel(8, 14 + lean, 3, 1, accentLight);
      pixel(10, 15 + lean, 1, 1, accentDark);
      pixel(11, 16 + lean, 1, 1, accentDark);
      pixel(8, 11 + lean, 3, 3, 'rgba(125,211,252,0.18)');
      pixel(2, 9 + lean, 1, 4, accent);
      pixel(9, 9 + lean, 1, 3, accent);
    } else {
      pixel(2, 9 + lean, 1, 5, accent);
      pixel(9, 9 + lean, 1, 5, accent);
      pixel(2, 13 + lean, 1, 2, skin.base);
      pixel(9, 13 + lean, 1, 2, skin.base);
    }

    if (state === 'awaiting') drawBubble(pixel, phase);
    else if (state === 'blocked') drawWarningBadge(pixel);
    else if (state === 'completed') drawCheckBadge(pixel);
    else if (state === 'failed') drawCrossBadge(pixel);
    else if (state === 'unknown') drawQuestionBadge(pixel);

    if (warning) {
      pixel(10, 8 + lean, 2, 2, BADGE.warning);
      pixel(10, 8 + lean, 1, 1, '#fff6dd');
    }
  }

  function drawHair(pixel, color, style, back) {
    const dark = mix(color, '#05070b', 0.28);
    if (style === 0) {
      pixel(3, 1, 6, 2, color);
      pixel(2, 2, 1, 3, color);
      pixel(9, 2, 1, 3, color);
    } else if (style === 1) {
      pixel(3, 1, 6, 2, color);
      pixel(2, 2, 1, 6, color);
      pixel(9, 2, 1, 6, color);
      pixel(2, 7, 2, 1, dark);
      pixel(8, 7, 2, 1, dark);
    } else if (style === 2) {
      pixel(3, 1, 6, 2, color);
      pixel(4, -1, 4, 2, color);
      pixel(2, 3, 1, 2, color);
      pixel(9, 3, 1, 2, color);
    } else if (style === 3) {
      pixel(2, 0, 8, 2, color);
      pixel(2, 2, 1, 2, color);
      pixel(9, 2, 1, 2, color);
      pixel(3, 0, 1, 1, dark);
      pixel(6, 0, 1, 1, dark);
    } else {
      pixel(3, 1, 6, 1, color);
      pixel(2, 2, 7, 1, color);
      pixel(9, 3, 1, 2, color);
    }
    if (back) {
      pixel(3, 2, 6, 4, color);
      pixel(3, 6, 6, 1, dark);
    }
  }

  function drawAccessory(pixel, accessory, accent, accentDark, back) {
    if (accessory === 'glasses' && !back) {
      pixel(3, 4, 2, 1, '#1d2733');
      pixel(7, 4, 2, 1, '#1d2733');
      pixel(5, 4, 2, 1, '#1d2733');
      pixel(4, 4, 1, 1, '#bcd7e8');
      pixel(8, 4, 1, 1, '#bcd7e8');
    } else if (accessory === 'headphones') {
      pixel(2, 0, 8, 1, '#161b23');
      pixel(1, 2, 2, 4, accentDark);
      pixel(9, 2, 2, 4, accentDark);
      pixel(1, 3, 1, 2, accent);
      pixel(10, 3, 1, 2, accent);
    } else if (accessory === 'cap') {
      pixel(3, 0, 6, 2, accentDark);
      pixel(2, 2, 8, 1, accent);
      pixel(1, 2, 2, 1, accentDark);
    } else if (accessory === 'bun') {
      pixel(4, -2, 4, 2, '#241d1a');
      pixel(5, -3, 2, 1, '#241d1a');
    } else if (accessory === 'beard' && !back) {
      pixel(3, 5, 6, 2, '#2a211b');
      pixel(4, 6, 4, 1, '#3a2d24');
    } else if (accessory === 'earring') {
      pixel(9, 6, 1, 1, '#f2c96a');
    }
  }

  function drawBubble(pixel, phase) {
    const tone = '#111720';
    pixel(6, -2, 6, 5, tone);
    pixel(5, -1, 8, 3, tone);
    pixel(10, 3, 2, 2, tone);
    pixel(7, 0, 1, 1, BADGE.awaiting);
    pixel(9, 0, 1, 1, phase === 1 ? '#fff1cf' : BADGE.awaiting);
    pixel(11, 0, 1, 1, BADGE.awaiting);
  }

  function drawWarningBadge(pixel) {
    const tone = '#1a1206';
    pixel(8, -3, 5, 5, tone);
    pixel(10, -2, 1, 2, BADGE.blocked);
    pixel(10, 1, 1, 1, BADGE.blocked);
    pixel(9, 1, 1, 1, BADGE.blocked);
    pixel(11, 1, 1, 1, BADGE.blocked);
  }

  function drawCheckBadge(pixel) {
    const tone = '#07140d';
    pixel(8, -3, 5, 5, tone);
    pixel(9, 0, 1, 1, BADGE.completed);
    pixel(10, -1, 1, 1, BADGE.completed);
    pixel(11, -2, 1, 1, BADGE.completed);
  }

  function drawCrossBadge(pixel) {
    const tone = '#170909';
    pixel(8, -3, 5, 5, tone);
    pixel(9, -2, 1, 1, BADGE.failed);
    pixel(11, -2, 1, 1, BADGE.failed);
    pixel(10, -1, 1, 1, BADGE.failed);
    pixel(9, 0, 1, 1, BADGE.failed);
    pixel(11, 0, 1, 1, BADGE.failed);
  }

  function drawQuestionBadge(pixel) {
    const tone = '#11151b';
    pixel(8, -3, 5, 5, tone);
    pixel(9, -2, 3, 1, BADGE.unknown);
    pixel(11, -1, 1, 1, BADGE.unknown);
    pixel(10, 0, 1, 1, BADGE.unknown);
    pixel(10, 1, 1, 1, BADGE.unknown);
  }

  function hitBox({ x = 0, y = 0, scale = 1 } = {}) {
    const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
    return {
      x: x - (HIT_WIDTH / 2) * s,
      y: y - HIT_HEIGHT * s,
      w: HIT_WIDTH * s,
      h: HIT_HEIGHT * s,
    };
  }

  const api = {
    draw,
    variantFor,
    hitBox,
    metrics: Object.freeze({
      width: SPRITE_WIDTH,
      height: SPRITE_HEIGHT,
      hitWidth: HIT_WIDTH,
      hitHeight: HIT_HEIGHT,
    }),
    states: Object.freeze([
      'spawn', 'idle', 'walking', 'working', 'reading', 'testing', 'reviewing',
      'awaiting', 'blocked', 'completed', 'failed', 'unknown',
    ]),
  };

  if (typeof window !== 'undefined') window.AOAgentSprites = api;
  if (typeof globalThis !== 'undefined') globalThis.AOAgentSprites = api;
})();
