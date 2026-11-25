import { useMemo, useState } from "react";
import type { ReactElement } from "react";

import { countNoun } from "../text/count-noun";
import { CopyButton } from "../ui/copy-button";
import { ErrorNotice, Notice } from "../ui/notice";
import { toAnswerCards } from "./answer-cards";
import type { Card, CardGroup } from "./answer-cards";
import type { ToolAnswer } from "./data-api";

export interface ToolAnswerViewProps {
  readonly answer: ToolAnswer;
}

/** The longest raw JSON that is put on screen, in characters; the copy button takes all of it. */
const MAX_RAW = 200_000;

/**
 * What a tool answered: the request that was sent, then the answer as cards or as the JSON the
 * server sent. A refusal of the server is shown as its error. All of it is text.
 */
export function ToolAnswerView({ answer }: ToolAnswerViewProps): ReactElement {
  const [raw, setRaw] = useState(false);
  const json = useMemo(() => JSON.stringify(answer.body, null, 2) ?? "null", [answer.body]);
  return (
    <section className="answer" aria-label="Answer">
      <div className="answer-request">
        <code>{answer.request}</code>
        <span className="muted small">
          {answer.status === null ? "" : `HTTP ${String(answer.status)}`}
        </span>
      </div>
      {answer.error === null ? (
        <>
          <div className="actions">
            <ViewSwitch raw={raw} onRaw={setRaw} />
            <CopyButton text={json} label="Copy JSON" />
          </div>
          {raw ? <RawJson json={json} /> : <Cards body={answer.body} />}
        </>
      ) : (
        <ErrorNotice error={answer.error} />
      )}
    </section>
  );
}

interface ViewSwitchProps {
  readonly raw: boolean;
  readonly onRaw: (raw: boolean) => void;
}

function ViewSwitch({ raw, onRaw }: ViewSwitchProps): ReactElement {
  return (
    <div className="tabs" role="tablist" aria-label="How the answer is shown">
      <button
        type="button"
        role="tab"
        aria-selected={!raw}
        className={raw ? "tab" : "tab current"}
        onClick={() => {
          onRaw(false);
        }}
      >
        Cards
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={raw}
        className={raw ? "tab current" : "tab"}
        onClick={() => {
          onRaw(true);
        }}
      >
        Raw JSON
      </button>
    </div>
  );
}

interface RawJsonProps {
  readonly json: string;
}

function RawJson({ json }: RawJsonProps): ReactElement {
  const cut = json.length > MAX_RAW;
  return (
    <>
      <pre className="raw-json">
        <code>{cut ? json.slice(0, MAX_RAW) : json}</code>
      </pre>
      {cut ? <p className="muted small">The answer is longer than what is shown; Copy JSON takes all of it.</p> : null}
    </>
  );
}

interface CardsProps {
  readonly body: unknown;
}

function Cards({ body }: CardsProps): ReactElement {
  const cards = useMemo(() => toAnswerCards(body), [body]);
  const empty = cards.record === null && cards.groups.every((group) => group.cards.length === 0);
  return (
    <>
      {cards.notice === null ? null : <p className="muted small">{cards.notice}</p>}
      {empty ? <Notice tone="info" title="The answer holds no item. The raw JSON shows what came back." /> : null}
      {cards.record === null ? null : <CardView card={cards.record} />}
      {cards.groups.map((group) => (
        <GroupView key={group.name} group={group} />
      ))}
    </>
  );
}

interface GroupViewProps {
  readonly group: CardGroup;
}

function GroupView({ group }: GroupViewProps): ReactElement {
  return (
    <section className="card-group">
      <h4>
        {group.name} <span className="muted small">{countNoun(group.cards.length + group.more, "item")}</span>
      </h4>
      {group.cards.map((card, position) => (
        <CardView key={`${String(position)}:${card.title}`} card={card} />
      ))}
      {group.more > 0 ? (
        <p className="muted small">{String(group.more)} more are in the raw JSON.</p>
      ) : null}
    </section>
  );
}

interface CardViewProps {
  readonly card: Card;
}

function CardView({ card }: CardViewProps): ReactElement {
  return (
    <article className="card">
      {card.title === "" ? null : <p className="card-title">{card.title}</p>}
      {card.subtitle === "" ? null : <p className="muted small">{card.subtitle}</p>}
      {card.text === null ? null : <pre className="card-text">{card.text}</pre>}
      {card.facts.length === 0 ? null : (
        <dl className="card-facts">
          {card.facts.map((fact) => (
            <div key={fact.name} className="fact">
              <dt>{fact.name}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {card.groups.map((group) => (
        <GroupView key={group.name} group={group} />
      ))}
    </article>
  );
}
