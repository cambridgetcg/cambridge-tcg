/**
 * Set code → display name mapping for all TCG sets.
 * Covers One Piece (OP01–OP13, ST01–ST21, EB01–EB02, promos)
 * and Pokemon (SV series).
 */
const SET_NAMES = {
  // One Piece — Booster sets
  OP01: 'Romance Dawn',
  OP02: 'Paramount War',
  OP03: 'Pillars of Strength',
  OP04: 'Kingdoms of Intrigue',
  OP05: 'Awakening of the New Era',
  OP06: 'Wings of the Captain',
  OP07: 'The Future 500 Years From Now',
  OP08: 'Two Legends',
  OP09: 'The Four Emperors',
  OP10: 'Royal Bloodlines',
  OP11: 'Uta\'s Return',
  OP12: 'Emperors in the New World',
  OP13: 'The Dawn of the World',

  // One Piece — Starter decks
  ST01: 'Straw Hat Crew',
  ST02: 'Worst Generation',
  ST03: 'The Seven Warlords of the Sea',
  ST04: 'Animal Kingdom Pirates',
  ST05: 'One Piece Film Edition',
  ST06: 'Absolute Justice',
  ST07: 'Big Mom Pirates',
  ST08: 'Monkey D. Luffy',
  ST09: 'Yamato',
  ST10: 'The Three Captains',
  ST11: 'Uta',
  ST12: 'Zoro & Sanji',
  ST13: 'The Three Brothers',
  ST14: 'Tony Tony Chopper',
  ST15: 'RED Edward Newgate',
  ST16: 'GREEN Uta',
  ST17: 'BLUE Donquixote Doflamingo',
  ST18: 'PURPLE Monkey D. Luffy',
  ST19: 'BLACK Smoker',
  ST20: 'YELLOW Charlotte Katakuri',
  ST21: 'Gear 5',

  OP14: 'The Azure Sea\'s Seven',

  // One Piece — Starter decks (newer)
  ST22: 'Gear 5 Luffy VS Akainu',
  ST23: 'Shanks',
  ST24: 'Nami',
  ST25: 'Nico Robin',
  ST26: 'Trafalgar Law',
  ST27: 'Sabo',
  ST28: 'Portgas D. Ace',

  // One Piece — Extra boosters / promos
  EB01: 'Extra Booster: Memorial Collection',
  EB02: 'Extra Booster: Anime 25th Anniversary',
  EB04: 'Extra Booster: Egghead Crisis',
  P: 'Promo',
  PRB01: 'Premium Booster: ONE PIECE CARD THE BEST',
  PRB02: 'Premium Booster: ONE PIECE CARD THE BEST Vol. 2',

  // Pokemon — Scarlet & Violet
  SV1a: 'Triplet Beat',
  SV1s: 'Scarlet ex',
  SV1v: 'Violet ex',
  SV2a: 'Snow Hazard',
  SV2d: 'Clay Burst',
  SV2p: 'Pokemon Card 151',
  SV3: 'Ruler of the Black Flame',
  SV3a: 'Raging Surf',
  SV4: 'Ancient Roar',
  SV4a: 'Shiny Treasure ex',
  SV4k: 'Future Flash',
  SV5a: 'Crimson Haze',
  SV5k: 'Wild Force',
  SV5m: 'Cyber Judge',
  SV6: 'Mask of Change',
  SV6a: 'Night Wanderer',
  SV7: 'Stellar Crown',
  SV7a: 'Paradise Dragona',
  SV8: 'Super Electric Breaker',
  SV8a: 'Terastal Fest ex',
  SV9: 'Surging Sparks',
  SV9a: 'Battle Partners',
};

/**
 * Game prefix → display name
 */
const GAME_NAMES = {
  OP: 'One Piece',
  PKMN: 'Pokemon',
};

/**
 * Get display name for a set code, falling back to the code itself.
 */
function getSetName(setCode) {
  return SET_NAMES[setCode] || setCode;
}

/**
 * Get display name for a game prefix.
 */
function getGameName(gamePrefix) {
  return GAME_NAMES[gamePrefix] || gamePrefix;
}
