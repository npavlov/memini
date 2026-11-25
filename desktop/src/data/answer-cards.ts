// An answer of a read tool as cards a person can read. The cards are built from the shape of
// the JSON, not from a list of tools: a field the server adds shows up as a fact. Every value is
// the server's text; it is sanitized here and shown as text.

import { isObject } from "../json/read-json";
import type { JsonObject } from "../json/read-json";
import { displayBlock, displayText } from "../text/display-text";

export interface CardFact {
  readonly name: string;
  readonly value: string;
}

export interface Card {
  readonly title: string;
  readonly subtitle: string;
  /** The longer text of the item, in its own lines: a snippet, a message, a file's content. */
  readonly text: string | null;
  readonly facts: readonly CardFact[];
  readonly groups: readonly CardGroup[];
}

export interface CardGroup {
  /** The field of the answer the cards come from, such as `hits`. */
  readonly name: string;
  readonly cards: readonly Card[];
  /** How many more items the answer has than the cards shown. */
  readonly more: number;
}

export interface AnswerCards {
  /** The server's note that what follows is data. */
  readonly notice: string | null;
  /** The answer's own fields, when it is one record. */
  readonly record: Card | null;
  readonly groups: readonly CardGroup[];
}

const TITLE_KEYS = ["title", "statement", "qualified_name", "name", "display_name", "locator", "path"];
const TEXT_KEYS = ["snippet", "text", "content", "summary"];
const SUBTITLE_KEYS = ["kind", "source", "repository", "workspace", "author", "status"];
const NOTICE_KEY = "data_notice";
const MAX_CARDS = 100;
const MAX_DEPTH = 2;
const MAX_TEXT = 6000;
const MAX_FACT = 300;

type Scalar = string | number | boolean;

function isScalar(value: unknown): value is Scalar {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

function firstText(item: JsonObject, keys: readonly string[]): string | null {
  const key = keys.find((name) => typeof item[name] === "string" && item[name] !== "");
  return key === undefined ? null : key;
}

function factValue(value: unknown): string | null {
  if (isScalar(value)) {
    return displayText(String(value), MAX_FACT);
  }
  if (Array.isArray(value) && value.length > 0 && value.every(isScalar)) {
    return displayText(value.map(String).join(", "), MAX_FACT);
  }
  return null;
}

function groupOf(name: string, value: unknown, depth: number): CardGroup | null {
  const items = Array.isArray(value) ? value.filter(isObject) : isObject(value) ? [value] : [];
  if (items.length === 0 || depth >= MAX_DEPTH) {
    return null;
  }
  return {
    name: displayText(name, 60),
    cards: items.slice(0, MAX_CARDS).map((item) => cardOf(item, depth + 1)),
    more: Math.max(0, items.length - MAX_CARDS),
  };
}

function cardOf(item: JsonObject, depth: number): Card {
  const titleKey = firstText(item, TITLE_KEYS);
  const textKey = firstText(item, TEXT_KEYS);
  const subtitleKeys = SUBTITLE_KEYS.filter((key) => typeof item[key] === "string" && item[key] !== "");
  const used = new Set([titleKey, textKey, NOTICE_KEY, ...subtitleKeys]);
  const rest = Object.entries(item).filter(([key]) => !used.has(key));
  return {
    title: titleKey === null ? "" : displayText(String(item[titleKey]), 200),
    subtitle: subtitleKeys.map((key) => displayText(String(item[key]), 80)).join(" · "),
    text: textKey === null ? null : displayBlock(String(item[textKey]), MAX_TEXT),
    facts: rest
      .map(([name, value]) => ({ name: displayText(name, 60), value: factValue(value) }))
      .filter((fact): fact is CardFact => fact.value !== null && fact.value !== ""),
    groups: rest
      .map(([name, value]) => groupOf(name, value, depth))
      .filter((group): group is CardGroup => group !== null),
  };
}

function hasContent(card: Card): boolean {
  return card.title !== "" || card.text !== null || card.facts.length > 0;
}

/** The cards of an answer. An answer that is not an object has none: its raw JSON says it all. */
export function toAnswerCards(body: unknown): AnswerCards {
  if (!isObject(body)) {
    return { notice: null, record: null, groups: [] };
  }
  const notice = body[NOTICE_KEY];
  const card = cardOf(body, 0);
  return {
    notice: typeof notice === "string" && notice !== "" ? displayText(notice, 400) : null,
    record: hasContent(card) ? { ...card, groups: [] } : null,
    groups: card.groups,
  };
}
