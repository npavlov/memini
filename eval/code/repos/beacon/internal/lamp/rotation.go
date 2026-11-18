package lamp

import (
	"errors"
	"time"
)

// RotationPeriod — время одного оборота линзы по умолчанию.
const RotationPeriod = 10 * time.Second

// ErrStalled означает, что линза перестала вращаться.
var ErrStalled = errors.New("lens stalled")

// Lamp описывает вращающуюся линзу маяка и её последний оборот.
type Lamp struct {
	Period   time.Duration
	lastTurn time.Time
	turns    int
}

// New создаёт линзу с заданным периодом вращения.
func New(period time.Duration) *Lamp {
	return &Lamp{Period: period}
}

// Turn отмечает очередной полный оборот линзы.
func (l *Lamp) Turn(at time.Time) {
	l.lastTurn = at
	l.turns++
}

// CheckRotation возвращает ErrStalled, если с последнего оборота прошло больше трёх периодов:
// значит, мотор линзы остановился и маяк не мигает.
func (l *Lamp) CheckRotation(now time.Time) error {
	if l.turns == 0 {
		return nil
	}
	if now.Sub(l.lastTurn) > 3*l.Period {
		return ErrStalled
	}
	return nil
}

// FlashesPerMinute считает число вспышек в минуту для характеристики Fl(n).
func FlashesPerMinute(flashesPerPeriod int, period time.Duration) float64 {
	return float64(flashesPerPeriod) * float64(time.Minute) / float64(period)
}
