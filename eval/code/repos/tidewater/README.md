# Tidewater

Tidewater schedules the berths of a small tidal harbour: which vessel goes to which berth, when
the water is deep enough to enter, and what the stay costs. It is a synthetic project, written for
the retrieval eval of Memini; the harbour does not exist.

## Install

Tidewater needs Python 3.12 and nothing outside the standard library. Install it into a virtual
environment of its own:

    python -m venv .venv
    .venv/bin/pip install -e .

The command `tidewater` is then on the path of the environment. Run `tidewater --help` to check
that it starts.

## Configuration

Settings live in `tidewater.toml` in the working directory. Every setting has a default except
the tide table, which the scheduler cannot work without. Unknown keys are an error, so that a typo
does not pass unnoticed.

### Tide tables

The tide table is a CSV file with one row per high and low water: the time in UTC and the height
in metres above chart datum. Set `tide_table = "tables/2026.csv"`. Heights between two rows are
interpolated in a straight line, which is close enough for a harbour this small.

A day missing from the table makes the scheduler refuse every arrival on that day rather than
guess the water.

### Berth limits

Each berth has a length, a depth at low water and a flag for hazardous cargo. A vessel longer
than the berth, or one whose draught plus the safety margin of half a metre is more than the
depth at low water, is never assigned to it.

Hazardous cargo goes only to berths that accept it, whatever else is free.

## Running the scheduler

`tidewater plan --day 2026-05-01` prints the plan of one day: each arrival with its berth and the
window in which it may enter. `tidewater assign VESSEL --length L --draught D` assigns a single
arrival at once and prints the berth, or the reason no berth fits.

Both commands read the configuration again on every run; there is no daemon to restart.

## Fees

Berth hire is charged per started hour, by the length of the vessel: a stay of one hour and ten
minutes is charged as two hours. Hours that start at night, from ten in the evening to six in
the morning, cost a quarter more.

Pilotage is charged once per entry: a flat part and a part by length. The rates are in
`fees.toml`; the invoice lists berth hire, the night surcharge and pilotage on separate lines.

## Troubleshooting

If the scheduler assigns nothing, check first that the tide table covers the day. Then check that
the draught of the vessel is given in metres, not in feet: a draught of 20 reads as twenty metres,
deeper than any berth of the harbour.

If the tide service does not answer, the scheduler retries with growing pauses and gives up after
five attempts.
