import { useMemo, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Card,
  CodeBlock,
  CopyButton,
  EmptyState,
  ErrorNotice,
  Fact,
  Facts,
  Section,
  Stack,
  Tabs,
  Text,
} from "../components";
import { countNoun } from "../text/count-noun";
import { toAnswerCards } from "./answer-cards";
import type { Card as AnswerCard, CardGroup } from "./answer-cards";
import type { ToolAnswer } from "./data-api";

export interface ToolAnswerViewProps {
  readonly answer: ToolAnswer;
}

/** The longest raw JSON that is put on screen, in characters; the copy button takes all of it. */
const MAX_RAW = 200_000;

const VIEWS = [
  { id: "cards", label: "Cards" },
  { id: "raw", label: "Raw JSON" },
];

/**
 * What a tool answered: the request that was sent, then the answer as cards or as the JSON the
 * server sent. A refusal of the server is shown as its error. All of it is text.
 */
export function ToolAnswerView({ answer }: ToolAnswerViewProps): ReactElement {
  const [view, setView] = useState("cards");
  const json = useMemo(() => JSON.stringify(answer.body, null, 2) ?? "null", [answer.body]);
  return (
    <Stack>
      <CodeBlock text={answer.request} wrap />
      {answer.status === null ? null : (
        <Text tone="muted" small>
          HTTP {String(answer.status)}
        </Text>
      )}
      {answer.error === null ? (
        <>
          <Actions>
            <Tabs label="How the answer is shown" items={VIEWS} current={view} onSelect={setView} />
            <CopyButton text={json} label="Copy JSON" />
          </Actions>
          {view === "raw" ? <RawJson json={json} /> : <Cards body={answer.body} />}
        </>
      ) : (
        <ErrorNotice error={answer.error} />
      )}
    </Stack>
  );
}

interface RawJsonProps {
  readonly json: string;
}

function RawJson({ json }: RawJsonProps): ReactElement {
  const cut = json.length > MAX_RAW;
  return (
    <>
      <CodeBlock text={cut ? json.slice(0, MAX_RAW) : json} bounded />
      {cut ? (
        <Text tone="muted" small>
          The answer is longer than what is shown; Copy JSON takes all of it.
        </Text>
      ) : null}
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
      {cards.notice === null ? null : (
        <Text tone="muted" small>
          {cards.notice}
        </Text>
      )}
      {cards.hidden === 0 ? null : (
        <Text tone="muted" small>
          {String(cards.hidden)} hidden (sensitive): records marked sensitive are left out unless a read asks for
          them with a token that has the sensitive scope.
        </Text>
      )}
      {empty ? (
        <EmptyState title="The answer holds no item">
          <Text>The raw JSON shows what came back.</Text>
        </EmptyState>
      ) : null}
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
  const count = (
    <Text tone="muted" small inline>
      {countNoun(group.cards.length + group.more, "item")}
    </Text>
  );
  return (
    <Section title={group.name} aside={count}>
      {group.cards.map((card, position) => (
        <CardView key={`${String(position)}:${card.title}`} card={card} />
      ))}
      {group.more > 0 ? (
        <Text tone="muted" small>
          {String(group.more)} more are in the raw JSON.
        </Text>
      ) : null}
    </Section>
  );
}

interface CardViewProps {
  readonly card: AnswerCard;
}

function CardView({ card }: CardViewProps): ReactElement {
  return (
    <Card title={card.title} subtitle={card.subtitle}>
      {card.text === null ? null : <CodeBlock text={card.text} wrap bounded />}
      {card.facts.length === 0 ? null : (
        <Facts>
          {card.facts.map((fact) => (
            <Fact key={fact.name} name={fact.name}>
              {fact.value}
            </Fact>
          ))}
        </Facts>
      )}
      {card.groups.map((group) => (
        <GroupView key={group.name} group={group} />
      ))}
    </Card>
  );
}
