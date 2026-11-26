import { describe, expect, it } from "vitest";

import { answeredBy } from "./use-load";

const noop = (): void => undefined;

describe("a reload that waits for its answer", () => {
  it("is answered by the round it asked for, and by none before it", () => {
    const waiting = [
      { round: 2, resolve: noop },
      { round: 3, resolve: noop },
    ];

    const { answered, still } = answeredBy(waiting, 2);

    expect(answered.map((reload) => reload.round)).toEqual([2]);
    expect(still.map((reload) => reload.round)).toEqual([3]);
  });

  it("is answered by a later round when the one it asked for was overtaken", () => {
    const { answered, still } = answeredBy([{ round: 4, resolve: noop }], 5);

    expect(answered.map((reload) => reload.round)).toEqual([4]);
    expect(still).toEqual([]);
  });

  it("is not answered by a round that settled before it was asked", () => {
    const { answered } = answeredBy([{ round: 7, resolve: noop }], 6);

    expect(answered).toEqual([]);
  });
});
