// What the front end may ask of the Rust side. This is the whole of it: the front end makes no
// HTTP request, runs no program and never holds a token.

export type CommandArguments = Readonly<Record<string, unknown>>;

export interface Backend {
  /** Calls a command of the Rust side by its name. Rejects with an AppError. */
  readonly invoke: (command: string, args?: CommandArguments) => Promise<unknown>;
  readonly copyText: (text: string) => Promise<void>;
  /** Copies a secret: clipboard managers are asked to keep it out of their history. */
  readonly copySecret: (text: string) => Promise<void>;
  /** The clock, in milliseconds since the epoch. */
  readonly now: () => number;
}
