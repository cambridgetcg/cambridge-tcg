/**
 * Set code → display name mapping for One Piece TCG sets.
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

  // One Piece — Extra boosters / promos
  EB01: 'Extra Booster: Memorial Collection',
  EB02: 'Extra Booster: Anime 25th Anniversary',
  PRB01: 'Premium Booster: ONE PIECE CARD THE BEST',
};

/**
 * Get display name for a set code, falling back to the code itself.
 */
function getSetName(setCode) {
  return SET_NAMES[setCode] || setCode;
}
