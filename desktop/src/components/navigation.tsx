import type { ReactElement, ReactNode } from "react";

import { Glyph } from "./icons";
import type { SelectOption } from "./inputs";
import { Text } from "./layout";

export interface SidebarProps {
  /** The items of the navigation. */
  readonly children: ReactNode;
  /** What stands under the brand, above the items: a choice that holds for every screen. */
  readonly header?: ReactNode;
  /** A line at the bottom, such as the app's version. */
  readonly footer?: string;
}

/** The app's navigation: the brand, the screens, and a line at the bottom. */
export function Sidebar({ children, header, footer }: SidebarProps): ReactElement {
  return (
    <nav className="sidebar" aria-label="Screens">
      <Brand />
      {header === undefined || header === null ? null : <div className="sidebar-header">{header}</div>}
      {children}
      {footer === undefined || footer === "" ? null : <p className="sidebar-footer">{footer}</p>}
    </nav>
  );
}

/** The glyph and the name, as they stand at the top of the sidebar. The name is set in mono. */
export function Brand(): ReactElement {
  return (
    <p className="brand">
      <Glyph size={22} />
      <span className="brand-name">memini</span>
    </p>
  );
}

export interface NavItemProps {
  readonly current: boolean;
  readonly onSelect: () => void;
  readonly children: ReactNode;
}

/** One screen of the navigation. */
export function NavItem({ current, onSelect, children }: NavItemProps): ReactElement {
  return (
    <button
      type="button"
      className={current ? "nav-item nav-item-current" : "nav-item"}
      aria-current={current ? "page" : undefined}
      onClick={onSelect}
    >
      {children}
    </button>
  );
}

export interface TabItem {
  readonly id: string;
  readonly label: string;
}

export interface TabsProps {
  /** What the tabs choose between, for a screen reader. */
  readonly label: string;
  readonly items: readonly TabItem[];
  readonly current: string;
  readonly onSelect: (id: string) => void;
}

/** A row of choices of which one is shown. What it shows is the caller's to render. */
export function Tabs({ label, items, current, onSelect }: TabsProps): ReactElement {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={item.id === current}
          className={item.id === current ? "tab tab-current" : "tab"}
          onClick={() => {
            onSelect(item.id);
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

export interface SelectableRowProps {
  readonly selected: boolean;
  readonly onSelect: () => void;
  readonly title: string;
  /** A line under the title, such as an address. */
  readonly detail?: ReactNode;
  /** What stands at the end of the row: chips, a button. */
  readonly children?: ReactNode;
}

/** One row of a list from which one item is chosen to be shown. */
export function SelectableRow(props: SelectableRowProps): ReactElement {
  const { selected, onSelect, title, detail, children } = props;
  return (
    <li className={selected ? "row row-selected" : "row"}>
      <button type="button" className="row-main" aria-pressed={selected} onClick={onSelect}>
        <strong>{title}</strong>
        {detail === undefined ? null : <Text tone="muted" inline>{detail}</Text>}
      </button>
      {children}
    </li>
  );
}

export interface RowListProps {
  readonly children: ReactNode;
}

/** The list that SelectableRows stand in. */
export function RowList({ children }: RowListProps): ReactElement {
  return <ul className="rows">{children}</ul>;
}

export interface SidebarPickerProps {
  /** What is chosen, such as "Workspace". */
  readonly label: string;
  readonly value: string;
  readonly options: readonly SelectOption[];
  readonly onChange: (value: string) => void;
  /** A line under the choice on what it means. */
  readonly hint?: string;
}

/** A choice in the sidebar that every screen follows, in the sidebar's colours. */
export function SidebarPicker({ label, value, options, onChange, hint }: SidebarPickerProps): ReactElement {
  return (
    <label className="sidebar-picker">
      <span className="sidebar-picker-label">{label}</span>
      <select
        className="sidebar-picker-select"
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint === undefined || hint === "" ? null : <small className="sidebar-picker-hint">{hint}</small>}
    </label>
  );
}
