package lazily

// Rate-shaping source operators (#lzrateshape) — the Go port of lazily-rs
// `src/rateshape.rs`. Each operator is a pure compute CORE — the emit/drop
// decision over plain state — split from a thin reactive CELL that projects the
// last emitted value onto a Cell[Opt[T]] so a dropped/held input never
// invalidates dependents. Time is the same monotone logical clock as #lztime.
//
// Only the NEW source operators live here: Debounce / Throttle / Sample /
// ProbabilisticSample. The lifted relay policies (Window/Expiry/Rate) already
// live in the relay plane and are not duplicated.

// Opt is a comparable optional value, the analogue of rs `Option<T>`. It can be
// stored in a Cell (which requires a comparable T) so the reader projection gets
// the PartialEq store-guard for free: emitting the same value twice does not
// cascade, and a dropped input never touches the cell.
type Opt[T comparable] struct {
	Present bool
	Value   T
}

// Some builds a present Opt.
func Some[T comparable](v T) Opt[T] { return Opt[T]{Present: true, Value: v} }

// None builds an absent Opt.
func None[T comparable]() Opt[T] { return Opt[T]{} }

// OptStr is the Option<string> projection the rateshape fixtures exercise.
type OptStr = Opt[string]

func rsMax64(a, b uint64) uint64 {
	if a > b {
		return a
	}
	return b
}

func rsClamp01(v float64) float64 {
	if v < 0.0 {
		return 0.0
	}
	if v > 1.0 {
		return 1.0
	}
	return v
}

// setOutput mirrors rs `set_output`: only an actual emit projects onto the
// reader cell. A dropped/held input leaves the last emitted value in place.
func setOutput[T comparable](cell *Cell[Opt[T]], emitted Opt[T]) {
	if emitted.Present {
		cell.Set(emitted)
	}
}

// -- Debounce ---------------------------------------------------------------

// DebounceCore coalesces inputs (KeepLatest) and emits the latest value only
// after `quiet` ticks with no new input — every input resets the deadline.
type DebounceCore[T comparable] struct {
	quiet   uint64
	pending Opt[T]
	fireAt  uint64
	armed   bool
}

// NewDebounceCore builds a debounce core with the given quiet period.
func NewDebounceCore[T comparable](quiet uint64) *DebounceCore[T] {
	return &DebounceCore[T]{quiet: quiet}
}

// Input records an input; resets the quiet deadline to now + quiet.
func (d *DebounceCore[T]) Input(now uint64, v T) {
	d.pending = Some(v)
	d.fireAt = now + d.quiet
	d.armed = true
}

// Tick advances; emits the latest value once the quiet period has elapsed.
func (d *DebounceCore[T]) Tick(now uint64) Opt[T] {
	if d.armed && d.pending.Present && d.fireAt <= now {
		d.armed = false
		emitted := d.pending
		d.pending = None[T]()
		return emitted
	}
	return None[T]()
}

// DebounceCell is the reactive debounce over any comparable-valued source.
type DebounceCell[T comparable] struct {
	core   *DebounceCore[T]
	output *Cell[Opt[T]]
}

// NewDebounceCell builds a reactive debounce bound to ctx.
func NewDebounceCell[T comparable](ctx *Context, quiet uint64) *DebounceCell[T] {
	return &DebounceCell[T]{core: NewDebounceCore[T](quiet), output: NewCell(ctx, None[T]())}
}

// Input buffers an input; does not emit.
func (c *DebounceCell[T]) Input(now uint64, v T) { c.core.Input(now, v) }

// Tick advances the clock and returns the emitted value (if any), projecting it
// onto the output reader.
func (c *DebounceCell[T]) Tick(now uint64) Opt[T] {
	emitted := c.core.Tick(now)
	setOutput(c.output, emitted)
	return emitted
}

// Output returns the last emitted value (subscribes the current computation).
func (c *DebounceCell[T]) Output() Opt[T] { return c.output.Get() }

// OutputCell exposes the reader cell for invalidation observation.
func (c *DebounceCell[T]) OutputCell() *Cell[Opt[T]] { return c.output }

// -- Throttle ---------------------------------------------------------------

// ThrottleEdge selects which edge of the window a ThrottleCore emits on.
type ThrottleEdge int

const (
	// ThrottleLeading: first input of a window passes immediately; rest dropped.
	ThrottleLeading ThrottleEdge = iota
	// ThrottleTrailing: first input opens the window; the latest is emitted at
	// the window boundary.
	ThrottleTrailing
)

// ThrottleCore emits at most one value per `window`.
type ThrottleCore[T comparable] struct {
	edge        ThrottleEdge
	window      uint64
	windowEnd   Opt[uint64] // Leading: end of the currently-open window.
	windowStart Opt[uint64] // Trailing: start of the currently-open window.
	pending     Opt[T]      // Trailing: coalesced latest.
}

// NewThrottleCore builds a throttle core.
func NewThrottleCore[T comparable](edge ThrottleEdge, window uint64) *ThrottleCore[T] {
	return &ThrottleCore[T]{edge: edge, window: window}
}

// Input records an input. Leading emits (or drops); Trailing coalesces and holds.
func (c *ThrottleCore[T]) Input(now uint64, v T) Opt[T] {
	switch c.edge {
	case ThrottleLeading:
		if c.windowEnd.Present && now < c.windowEnd.Value {
			return None[T]()
		}
		c.windowEnd = Some[uint64](now + c.window)
		return Some(v)
	default: // ThrottleTrailing
		if !c.windowStart.Present {
			c.windowStart = Some(now)
		}
		c.pending = Some(v)
		return None[T]()
	}
}

// Tick advances. Trailing emits the coalesced latest at the window boundary.
func (c *ThrottleCore[T]) Tick(now uint64) Opt[T] {
	if c.edge == ThrottleLeading {
		return None[T]()
	}
	if !c.windowStart.Present {
		return None[T]()
	}
	ws := c.windowStart.Value
	if now >= ws+c.window && c.pending.Present {
		c.windowStart = None[uint64]()
		emitted := c.pending
		c.pending = None[T]()
		return emitted
	}
	return None[T]()
}

// ThrottleCell is the reactive throttle over any comparable-valued source.
type ThrottleCell[T comparable] struct {
	core   *ThrottleCore[T]
	output *Cell[Opt[T]]
}

// NewThrottleCell builds a reactive throttle bound to ctx.
func NewThrottleCell[T comparable](ctx *Context, edge ThrottleEdge, window uint64) *ThrottleCell[T] {
	return &ThrottleCell[T]{core: NewThrottleCore[T](edge, window), output: NewCell(ctx, None[T]())}
}

// Input records an input, returning the emitted value (if any).
func (c *ThrottleCell[T]) Input(now uint64, v T) Opt[T] {
	emitted := c.core.Input(now, v)
	setOutput(c.output, emitted)
	return emitted
}

// Tick advances the clock, returning the emitted value (if any).
func (c *ThrottleCell[T]) Tick(now uint64) Opt[T] {
	emitted := c.core.Tick(now)
	setOutput(c.output, emitted)
	return emitted
}

// Output returns the last emitted value (subscribes the current computation).
func (c *ThrottleCell[T]) Output() Opt[T] { return c.output.Get() }

// OutputCell exposes the reader cell for invalidation observation.
func (c *ThrottleCell[T]) OutputCell() *Cell[Opt[T]] { return c.output }

// -- Sample -----------------------------------------------------------------

// SampleKind selects count-based vs time-based sampling.
type SampleKind int

const (
	// SampleCountKind: emit every n-th input.
	SampleCountKind SampleKind = iota
	// SampleTimeKind: emit the held latest at each period boundary.
	SampleTimeKind
)

// SampleMode is the sampling mode for SampleCore — the Go analogue of rs
// `SampleMode::Count(n)` / `SampleMode::Time(period)`.
type SampleMode struct {
	Kind   SampleKind
	N      uint64
	Period uint64
}

// SampleCount builds a count-based mode (emit every n-th input).
func SampleCount(n uint64) SampleMode { return SampleMode{Kind: SampleCountKind, N: n} }

// SampleTime builds a time-based mode (emit at each period boundary).
func SampleTime(period uint64) SampleMode { return SampleMode{Kind: SampleTimeKind, Period: period} }

// SampleCore is the deterministic sampling compute core.
type SampleCore[T comparable] struct {
	mode    SampleMode
	counter uint64
	next    uint64
	held    Opt[T]
}

// NewSampleCore builds a sampling core.
func NewSampleCore[T comparable](mode SampleMode) *SampleCore[T] {
	next := uint64(0)
	if mode.Kind == SampleTimeKind {
		next = rsMax64(mode.Period, 1)
	}
	return &SampleCore[T]{mode: mode, next: next}
}

// Input records an input. Count mode emits on every n-th; Time mode holds the
// latest for the next boundary.
func (c *SampleCore[T]) Input(v T) Opt[T] {
	if c.mode.Kind == SampleCountKind {
		n := rsMax64(c.mode.N, 1)
		c.counter++
		if c.counter%n == 0 {
			return Some(v)
		}
		return None[T]()
	}
	// Time mode: hold the latest for the next boundary.
	c.held = Some(v)
	return None[T]()
}

// Tick advances. Time mode emits the held latest once per period boundary
// crossed.
func (c *SampleCore[T]) Tick(now uint64) Opt[T] {
	if c.mode.Kind == SampleCountKind {
		return None[T]()
	}
	period := rsMax64(c.mode.Period, 1)
	if now < c.next {
		return None[T]()
	}
	fires := (now-c.next)/period + 1
	c.next += fires * period
	// Emit the held latest; it persists (sampling the current value).
	return c.held
}

// SampleCell is the reactive sampler over any comparable-valued source.
type SampleCell[T comparable] struct {
	core   *SampleCore[T]
	output *Cell[Opt[T]]
}

// NewSampleCell builds a reactive sampler bound to ctx.
func NewSampleCell[T comparable](ctx *Context, mode SampleMode) *SampleCell[T] {
	return &SampleCell[T]{core: NewSampleCore[T](mode), output: NewCell(ctx, None[T]())}
}

// Input records an input, returning the emitted value (if any).
func (c *SampleCell[T]) Input(v T) Opt[T] {
	emitted := c.core.Input(v)
	setOutput(c.output, emitted)
	return emitted
}

// Tick advances the clock, returning the emitted value (if any).
func (c *SampleCell[T]) Tick(now uint64) Opt[T] {
	emitted := c.core.Tick(now)
	setOutput(c.output, emitted)
	return emitted
}

// Output returns the last emitted value (subscribes the current computation).
func (c *SampleCell[T]) Output() Opt[T] { return c.output.Get() }

// OutputCell exposes the reader cell for invalidation observation.
func (c *SampleCell[T]) OutputCell() *Cell[Opt[T]] { return c.output }

// -- Probabilistic sample ----------------------------------------------------

// SampleRng is an injectable RNG so probabilistic sampling is deterministic
// under a fixed seed. NextFloat64 yields a draw in [0, 1).
type SampleRng interface {
	NextFloat64() float64
}

// Lcg is a small deterministic SplitMix64-style generator — no external
// dependency, reproducible for the distribution property test.
type Lcg struct {
	state uint64
}

// NewLcg builds a deterministic generator seeded with `seed`.
func NewLcg(seed uint64) *Lcg { return &Lcg{state: seed} }

// NextFloat64 returns the next draw in [0, 1). Go unsigned arithmetic wraps, so
// this matches rs `wrapping_add`/`wrapping_mul` bit-for-bit.
func (l *Lcg) NextFloat64() float64 {
	l.state += 0x9E3779B97F4A7C15
	z := l.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	z ^= z >> 31
	// 53-bit mantissa → [0, 1).
	return float64(z>>11) / float64(uint64(1)<<53)
}

// ProbabilisticSampleCore is the tail-sampling compute core. A draw in [0, 1)
// passes iff draw < rate.
type ProbabilisticSampleCore struct {
	rate float64
}

// NewProbabilisticSampleCore builds a core with rate clamped to [0, 1].
func NewProbabilisticSampleCore(rate float64) ProbabilisticSampleCore {
	return ProbabilisticSampleCore{rate: rsClamp01(rate)}
}

// Rate returns the (clamped) sampling rate.
func (c ProbabilisticSampleCore) Rate() float64 { return c.rate }

// Decide reports whether an input with this random draw is sampled.
func (c ProbabilisticSampleCore) Decide(draw float64) bool { return draw < c.rate }

// ProbabilisticSampleCell is the reactive probabilistic sampler; it owns an
// injectable SampleRng.
type ProbabilisticSampleCell[T comparable] struct {
	core   ProbabilisticSampleCore
	rng    SampleRng
	output *Cell[Opt[T]]
}

// NewProbabilisticSampleCell builds a reactive probabilistic sampler bound to
// ctx.
func NewProbabilisticSampleCell[T comparable](ctx *Context, rate float64, rng SampleRng) *ProbabilisticSampleCell[T] {
	return &ProbabilisticSampleCell[T]{
		core:   NewProbabilisticSampleCore(rate),
		rng:    rng,
		output: NewCell(ctx, None[T]()),
	}
}

// Input samples an input using the owned RNG.
func (c *ProbabilisticSampleCell[T]) Input(v T) Opt[T] {
	return c.InputWithDraw(v, c.rng.NextFloat64())
}

// InputWithDraw samples an input against an explicit draw (deterministic /
// conformance). Emits iff draw < rate.
func (c *ProbabilisticSampleCell[T]) InputWithDraw(v T, draw float64) Opt[T] {
	if c.core.Decide(draw) {
		c.output.Set(Some(v))
		return Some(v)
	}
	return None[T]()
}

// Output returns the last emitted value (subscribes the current computation).
func (c *ProbabilisticSampleCell[T]) Output() Opt[T] { return c.output.Get() }

// OutputCell exposes the reader cell for invalidation observation.
func (c *ProbabilisticSampleCell[T]) OutputCell() *Cell[Opt[T]] { return c.output }
