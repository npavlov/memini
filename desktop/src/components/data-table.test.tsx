import { describe, expect, it } from "vitest";

import { DataTable } from "./data-table";
import { markupOf } from "./markup";

const row = (
  <tr>
    <th scope="row">db</th>
    <td>running</td>
  </tr>
);

describe("DataTable", () => {
  it("heads each column and scrolls sideways instead of the page", () => {
    const markup = markupOf(<DataTable columns={["Service", "State"]}>{row}</DataTable>);

    expect(markup).toMatch(/^<div class="table-scroll"><table class="table"><thead><tr>/u);
    expect(markup).toContain('<th scope="col">Service</th><th scope="col">State</th>');
    expect(markup).toContain('<tbody><tr><th scope="row">db</th><td>running</td></tr></tbody>');
  });

  it("names the column of buttons for a screen reader without showing the name", () => {
    const markup = markupOf(
      <DataTable columns={["Name"]} withActions>
        {row}
      </DataTable>,
    );

    expect(markup).toContain('<th scope="col"><span class="visually-hidden">Actions</span></th>');
  });
});
