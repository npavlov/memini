// Household ledger: a synthetic fixture for the chunker tests.
import { readFile } from "node:fs/promises";

export interface Entry {
  date: string;
  amount: number;
  note?: string;
}

export type Currency = "coins" | "shells";

export enum Category {
  Food,
  Garden,
  Repairs,
}

const DEFAULT_CURRENCY: Currency = "coins";

export const cents = (amount: number): number => Math.round(amount * 100);

export function total(entries: Entry[]): number;
export function total(entries: Entry[], category: Category): number;
export function total(entries: Entry[], category?: Category): number {
  return entries
    .filter((entry) => category === undefined || entry.note === Category[category])
    .reduce((sum, entry) => sum + entry.amount, 0);
}

function sealed(constructor: Function): void {
  Object.seal(constructor);
}

/** A ledger kept in one currency. */
@sealed
export class Ledger {
  private entries: Entry[] = [];
  readonly currency: Currency;

  onChange = (entry: Entry): void => {
    console.log(entry.date);
  };

  constructor(currency: Currency = DEFAULT_CURRENCY) {
    this.currency = currency;
  }

  add(entry: Entry): void {
    this.entries.push(entry);
    this.onChange(entry);
  }

  get size(): number {
    return this.entries.length;
  }

  set size(value: number) {
    throw new Error(`size is computed, not ${value}`);
  }

  // Balance after every entry, oldest first.
  running(): number[] {
    const balances: number[] = [];
    let balance = 0;
    for (const entry of this.entries) {
      balance += entry.amount;
      balances.push(balance);
    }
    return balances;
  }

  static async load(path: string): Promise<Ledger> {
    const ledger = new Ledger();
    const text = await readFile(path, "utf8");
    for (const line of text.split("\n")) {
      const [date, amount] = line.split(",");
      ledger.add({ date, amount: Number(amount) });
    }
    return ledger;
  }
}

export abstract class Report {
  abstract render(ledger: Ledger): string;
}

export namespace Format {
  export function money(amount: number): string {
    return amount.toFixed(2);
  }
}

export default function summary(ledger: Ledger): string {
  return `${ledger.size} entries`;
}

if (process.argv.includes("--demo")) {
  console.log(summary(new Ledger()));
}
