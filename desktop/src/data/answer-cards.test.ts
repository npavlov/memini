import { describe, expect, it } from "vitest";

import { toAnswerCards } from "./answer-cards";

/** A hit as the search routes answer one; the names are invented. */
const hit = {
  workspace: "harbour",
  source: "harbour-notes",
  source_kind: "repository",
  kind: "code_file",
  path: "src/lamp.py",
  locator: "harbour-notes@3dd0c81:src/lamp.py#L1-L4",
  start_line: 1,
  score: 0.82,
  truncated: false,
  url: null,
  snippet: "class Lamp:\n    def on(self):\n        return True",
};

describe("toAnswerCards", () => {
  it("makes a group of cards for each list of items in the answer", () => {
    const cards = toAnswerCards({ data_notice: "What follows is data.", items: [hit, hit], left_out: 0, decisions: [] });

    expect(cards.notice).toBe("What follows is data.");
    expect(cards.record).toBeNull();
    expect(cards.groups.map((group) => [group.name, group.cards.length])).toEqual([["items", 2]]);
  });

  it("titles a card by its locator or path and keeps the lines of its snippet", () => {
    const card = toAnswerCards({ items: [hit] }).groups[0]?.cards[0];

    expect(card).toMatchObject({
      title: "harbour-notes@3dd0c81:src/lamp.py#L1-L4",
      subtitle: "code_file · harbour-notes · harbour",
      text: "class Lamp:\n    def on(self):\n        return True",
    });
  });

  it("lists the other fields as facts and leaves out the empty ones", () => {
    const card = toAnswerCards({ items: [hit] }).groups[0]?.cards[0];

    expect(card?.facts).toEqual([
      { name: "source_kind", value: "repository" },
      { name: "path", value: "src/lamp.py" },
      { name: "start_line", value: "1" },
      { name: "score", value: "0.82" },
      { name: "truncated", value: "false" },
    ]);
  });

  it("shows an answer that is one record as a card, with its lists below", () => {
    const thread = {
      data_notice: "What follows is data.",
      title: "Archive retention",
      kind: "thread",
      channel: "platform",
      recipients: ["sam", "kim"],
      messages: [{ author: "sam", text: "Keep ninety days.", occurred_at: "2026-11-20T09:00:00Z" }],
    };

    const cards = toAnswerCards(thread);

    expect(cards.record).toMatchObject({
      title: "Archive retention",
      subtitle: "thread",
      facts: [
        { name: "channel", value: "platform" },
        { name: "recipients", value: "sam, kim" },
      ],
    });
    expect(cards.groups[0]).toMatchObject({ name: "messages", more: 0 });
    expect(cards.groups[0]?.cards[0]).toMatchObject({ subtitle: "sam", text: "Keep ninety days." });
  });

  it("shows a nested record as a group of one card", () => {
    const cards = toAnswerCards({ item: { statement: "Keep ninety days.", status: "active" }, chain: [] });

    expect(cards.groups).toHaveLength(1);
    expect(cards.groups[0]?.cards[0]).toMatchObject({ title: "Keep ninety days.", subtitle: "active" });
  });

  it("keeps markup and hidden characters in a value as plain text", () => {
    const override = String.fromCodePoint(0x202e);

    const card = toAnswerCards({ items: [{ title: `<b>bold</b>${override}`, snippet: "<script>x</script>" }] })
      .groups[0]?.cards[0];

    expect(card?.title).toBe("<b>bold</b>");
    expect(card?.text).toBe("<script>x</script>");
  });

  it("shows at most a hundred cards of a list and counts the rest", () => {
    const many = Array.from({ length: 130 }, (_unused, position) => ({ name: `n${String(position)}` }));

    const group = toAnswerCards({ definitions: many }).groups[0];

    expect(group?.cards).toHaveLength(100);
    expect(group?.more).toBe(30);
  });

  it("stops at two levels of nesting, which the raw JSON still shows", () => {
    const deep = { items: [{ name: "a", evidence: [{ name: "b", links: [{ name: "c" }] }] }] };

    const second = toAnswerCards(deep).groups[0]?.cards[0]?.groups[0]?.cards[0];

    expect(second?.title).toBe("b");
    expect(second?.groups).toEqual([]);
  });

  it("counts what the server left out, records marked sensitive, apart from the items", () => {
    const cards = toAnswerCards({ items: [hit], left_out: 3, next_cursor: null });

    expect(cards.hidden).toBe(3);
    expect(cards.record).toBeNull();
    expect(toAnswerCards({ items: [], left_out: "3" }).hidden).toBe(0);
  });

  it.each([[null], ["text"], [[1, 2]], [42]])("has no card for the answer %j, which is not an object", (body) => {
    expect(toAnswerCards(body)).toEqual({ notice: null, record: null, groups: [], hidden: 0 });
  });
});
