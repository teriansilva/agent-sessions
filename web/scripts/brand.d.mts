// Types for brand.mjs (#1590), so brandAssets.test.ts can import the generator's own logic.
export const WEB: string;
export const REPO: string;
export const BRAND: string;
export const GROUND: string;
export function brandFiles(): Record<string, string>;
export const RASTERS: [string, string, number, number][];
export function brandSlots(): Record<string, string>;
export const SLOTTED: string[];
export function fillSlots(text: string): string;
export function slotNames(text: string): string[];
