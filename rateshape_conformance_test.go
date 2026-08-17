package lazily

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// Cross-language conformance for the rate-shaping source operators
// (#lzrateshape) — see lazily-spec/conformance/rateshape/*.json. Each fixture
// replays a stream of input/tick ops and asserts the emitted value (`returns`),
// the projected `output` reader, and that the `output` reader invalidates
// exactly on an emit (observed via a wrapping Slot's cache warmth — the Go
// analogue of rs `ctx.is_set`).

func rateshapeSpecDir() string {
	return filepath.Join("..", "lazily-spec", "conformance", "rateshape")
}

var rateshapeFixtureNames = []string{
	"debounce.json",
	"throttle_leading.json",
	"throttle_trailing.json",
	"sample_count.json",
	"sample_time.json",
	"probabilistic_sample.json",
}

type rateshapeOp struct {
	Type  string   `json:"type"`
	Now   *uint64  `json:"now"`
	Value string   `json:"value"`
	Draw  *float64 `json:"draw"`
}

type rateshapeExpected struct {
	Output      *string `json:"output"`
	Invalidates struct {
		Output bool `json:"output"`
	} `json:"invalidates"`
}

type rateshapeStep struct {
	Op       rateshapeOp       `json:"op"`
	Returns  *string           `json:"returns"`
	Expected rateshapeExpected `json:"expected"`
}

type rateshapeInitial struct {
	Quiet  *uint64  `json:"quiet"`
	Window *uint64  `json:"window"`
	Edge   string   `json:"edge"`
	Mode   string   `json:"mode"`
	N      *uint64  `json:"n"`
	Period *uint64  `json:"period"`
	Rate   *float64 `json:"rate"`
}

type rateshapeFixture struct {
	Initial rateshapeInitial `json:"initial"`
	Steps   []rateshapeStep  `json:"steps"`
}

func loadRateshapeFixture(t *testing.T, name string) (rateshapeFixture, bool) {
	t.Helper()
	path := filepath.Join(rateshapeSpecDir(), name)
	data, err := os.ReadFile(path)
	if err != nil {
		return rateshapeFixture{}, false
	}
	var fx rateshapeFixture
	if err := json.Unmarshal(data, &fx); err != nil {
		t.Fatalf("%s: unmarshal fixture: %v", name, err)
	}
	return fx, true
}

// optFromPtr converts a fixture *string (`returns` / `expected.output`, which is
// JSON null when absent) into an Opt[string].
func optFromPtr(p *string) OptStr {
	if p == nil {
		return None[string]()
	}
	return Some(*p)
}

// driveFn performs one op and returns the (emitted, current-output) pair.
type driveFn func(step rateshapeStep) (OptStr, OptStr)

// runRateshape replays a fixture: it drives each op, asserts emit + output, and
// asserts reader invalidation via a wrapping Slot's cache warmth.
func runRateshape(t *testing.T, ctx *Context, name string, fx rateshapeFixture, outCell *Cell[OptStr], drive driveFn) {
	t.Helper()
	observed := NewSlot(ctx, func(_ *Context) OptStr { return outCell.Get() })
	observed.Get() // prime

	for i, step := range fx.Steps {
		emitted, output := drive(step)

		if want := optFromPtr(step.Returns); emitted != want {
			t.Fatalf("%s: step %d emit: got %+v, want %+v", name, i, emitted, want)
		}
		if want := optFromPtr(step.Expected.Output); output != want {
			t.Fatalf("%s: step %d output: got %+v, want %+v", name, i, output, want)
		}

		_, warm := observed.Peek()
		observed.Get() // re-prime
		if invalidated := !warm; invalidated != step.Expected.Invalidates.Output {
			t.Fatalf("%s: step %d invalidates.output: got %v, want %v", name, i, invalidated, step.Expected.Invalidates.Output)
		}
	}
}

func replayRateshapeFixture(t *testing.T, name string, fx rateshapeFixture) {
	t.Helper()
	ctx := NewContext()

	switch name {
	case "debounce.json":
		cell := NewDebounceCell[string](ctx, *fx.Initial.Quiet)
		runRateshape(t, ctx, name, fx, cell.OutputCell(), func(step rateshapeStep) (OptStr, OptStr) {
			var emitted OptStr
			if step.Op.Type == "input" {
				cell.Input(*step.Op.Now, step.Op.Value)
				emitted = None[string]()
			} else {
				emitted = cell.Tick(*step.Op.Now)
			}
			return emitted, cell.Output()
		})

	case "throttle_leading.json", "throttle_trailing.json":
		edge := ThrottleLeading
		if fx.Initial.Edge == "Trailing" {
			edge = ThrottleTrailing
		}
		cell := NewThrottleCell[string](ctx, edge, *fx.Initial.Window)
		runRateshape(t, ctx, name, fx, cell.OutputCell(), func(step rateshapeStep) (OptStr, OptStr) {
			var emitted OptStr
			if step.Op.Type == "input" {
				emitted = cell.Input(*step.Op.Now, step.Op.Value)
			} else {
				emitted = cell.Tick(*step.Op.Now)
			}
			return emitted, cell.Output()
		})

	case "sample_count.json":
		cell := NewSampleCell[string](ctx, SampleCount(*fx.Initial.N))
		runRateshape(t, ctx, name, fx, cell.OutputCell(), func(step rateshapeStep) (OptStr, OptStr) {
			emitted := cell.Input(step.Op.Value)
			return emitted, cell.Output()
		})

	case "sample_time.json":
		cell := NewSampleCell[string](ctx, SampleTime(*fx.Initial.Period))
		runRateshape(t, ctx, name, fx, cell.OutputCell(), func(step rateshapeStep) (OptStr, OptStr) {
			var emitted OptStr
			if step.Op.Type == "input" {
				cell.Input(step.Op.Value)
				emitted = None[string]()
			} else {
				emitted = cell.Tick(*step.Op.Now)
			}
			return emitted, cell.Output()
		})

	case "probabilistic_sample.json":
		// Draws are injected per step via InputWithDraw, so the owned RNG is
		// unused here; a deterministic Lcg satisfies construction.
		cell := NewProbabilisticSampleCell[string](ctx, *fx.Initial.Rate, NewLcg(0))
		runRateshape(t, ctx, name, fx, cell.OutputCell(), func(step rateshapeStep) (OptStr, OptStr) {
			emitted := cell.InputWithDraw(step.Op.Value, *step.Op.Draw)
			return emitted, cell.Output()
		})

	default:
		t.Fatalf("unknown rateshape fixture: %s", name)
	}
}

func TestRateshapeConformance(t *testing.T) {
	for _, name := range rateshapeFixtureNames {
		name := name
		t.Run(name, func(t *testing.T) {
			fx, ok := loadRateshapeFixture(t, name)
			if !ok {
				t.Skipf("lazily-spec fixture absent: %s", filepath.Join(rateshapeSpecDir(), name))
			}
			replayRateshapeFixture(t, name, fx)
		})
	}
}
