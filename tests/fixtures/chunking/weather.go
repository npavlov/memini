// Package weather is a synthetic fixture for the chunker tests.
package weather

import (
	"errors"
	"fmt"
)

const Unit = "celsius"

const (
	Calm = iota
	Breeze
	Gale
)

var ErrNoReadings = errors.New("no readings")

// Reading is one measurement from a station.
type Reading struct {
	Station string
	Celsius float64
}

type Source interface {
	Readings() ([]Reading, error)
}

type (
	Station string
	Window  = []Reading
)

// Average is the mean temperature of the readings.
func Average(readings []Reading) (float64, error) {
	if len(readings) == 0 {
		return 0, ErrNoReadings
	}
	sum := 0.0
	for _, reading := range readings {
		sum += reading.Celsius
	}
	return sum / float64(len(readings)), nil
}

func (r Reading) String() string { return fmt.Sprintf("%s %.1f", r.Station, r.Celsius) }

func (r *Reading) Warm(by float64) { r.Celsius += by }

// Series keeps the last readings of any kind.
type Series[T any] struct {
	items []T
	limit int
}

func (s *Series[T]) Push(item T) {
	s.items = append(s.items, item)
	if len(s.items) > s.limit {
		s.items = s.items[1:]
	}
}

func NewSeries[T any](limit int) *Series[T] {
	return &Series[T]{limit: limit}
}

func init() { fmt.Println("weather loaded") }

func init() { fmt.Println("units:", Unit) }
