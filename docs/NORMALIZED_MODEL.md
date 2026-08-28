# Normalized workout model

The domain layer separates Zepp's transport schema from MCP output. It can be
used independently of the network client and is tested entirely with synthetic
fixtures.

## Workout identity

Zepp requires both `trackid` and `source` to fetch workout details. `WorkoutId`
stores both values and exposes a stable URL-safe `zepp:...` encoding for MCP
arguments. Decoding validates the structure and rejects empty components.

`trackid` is treated as the Unix workout start timestamp. This avoids deriving
an incorrect start from active duration when pauses are present.

## Output layers

1. `WorkoutSummary` contains normalized history fields and compact metadata.
2. `WorkoutDetail` adds sample and subdivision inventories while excluding bulk
   series.
3. `WorkoutSample` contains time-keyed route and sensor measurements.
4. `WorkoutSubdivision` contains splits, pool lengths and sets, rowing
   intervals, pauses, and strength-set records.

Samples and subdivisions are bounded independently. `select_samples` filters by
time and metric and can return at most 10,000 points. `select_subdivisions`
filters by kind and can return at most 1,000 records. MCP applies lower limits.

## Units

| Field | Unit |
| --- | --- |
| Duration and offsets | seconds |
| Distance | metres |
| Energy | kilojoules |
| Heart rate | beats per minute |
| Elevation and altitude | metres |
| Coordinates | decimal degrees |
| GPS accuracy | metres |
| Speed | metres per second |
| Pace | seconds per metre, kilometre, or 100 metres as named |
| Stride length | metres |
| Step frequency | steps per minute |
| Stroke rate | strokes per minute |

Training-effect integers are converted from tenths to `0.0`–`5.0` scores.
`currentDistance` is converted from absolute centimetres to metres.
`elevationGain` and `elevationLoss` are converted from centimetres and checked
against their integer-metre companion fields.

## Sample joining

Route points use accumulated deltas from `time`. Heart rate, speed, distance,
and gait are sparse change series joined on accumulated second offsets. The
last sparse value is forward-filled. Sparse changes without a route point still
produce a non-GPS sample, keeping indoor workouts useful.

Missing route positions retain their time slot and have null coordinates. The
normalizer does not invent coordinate interpolation.

## Supported workout types

Only evidence-backed IDs are named:

- `1`: running;
- `6`: walking;
- `14`: indoor pool swimming;
- `22`: hiking;
- `23`: rowing machine.

Other IDs remain `unknown` and retain their Zepp type and sport-mode IDs.
Recognized sport metrics live under `sport_metrics`; unclassified values live
under redacted and bounded `raw_metadata`.

## Subdivisions and timing

The verified portion of a distance-split record includes index, duration,
cumulative end offset, average heart rate, energy, and cadence or stroke rate.
The verified pool record fields include record kind, precise duration,
distance, heart rate, stroke type/rate/count, SWOLF, pace, and distance per
stroke.

Pool record kind `7` is an individual length and kind `5` an aggregate set.
Stroke type `1` is breaststroke and `2` freestyle; unrecognized IDs remain
`unknown`.

Aggregate timing fields do not always share the same clock as individual
records. Pool-set boundaries are therefore derived from ordered member lengths
after distance and stroke-count reconciliation. Rowing aggregate duration is
retained as active duration, while untrustworthy boundaries remain null.

Every emitted offset is checked against the workout wall clock (`end_time -
trackid`). Small whole-second versus millisecond discrepancies can be bounded
to the wall clock with `derived` provenance; larger conflicts cause boundaries
to be omitted.

Human-facing output rounds timestamps to milliseconds, distances to two
decimals, and stroke rates and display paces to one decimal. Raw source values
are not modified.

## Provenance

Normalized fields use these provenance markers:

- `zepp`: a direct named Zepp value;
- `decoded`: a value decoded from a documented positional or compact field;
- `derived`: a value calculated from other normalized values.

## Raw-data policy

Raw positional rows stay on internal models and are serialized only beneath
`raw_metadata` when debug output is explicitly requested. Normal MCP
subdivision responses omit them.

The tracked fixtures are fully synthetic and contain no account identifiers,
devices, coordinates, credentials, or personal workout measurements.
