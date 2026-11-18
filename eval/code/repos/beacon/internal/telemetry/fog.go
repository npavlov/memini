package telemetry

import "time"

// Пороги видимости туманного горна в метрах: включается ниже двух морских миль, выключается
// только выше двух с половиной, чтобы горн не щёлкал на границе.
const (
	FogOnMeters  = 3704
	FogOffMeters = 4630
)

// BlastLength и BlastInterval задают сигнал горна: гудок три секунды каждые тридцать секунд.
const (
	BlastLength   = 3 * time.Second
	BlastInterval = 30 * time.Second
)

// ShouldSoundHorn решает, нужен ли туманный сигнал при данной видимости. Горн, который уже
// звучит, выключается только при видимости выше FogOffMeters (гистерезис).
func ShouldSoundHorn(visibilityMeters int, hornOn bool) bool {
	if hornOn {
		return visibilityMeters < FogOffMeters
	}
	return visibilityMeters < FogOnMeters
}

// HornDuty — доля времени, в течение которой горн звучит.
func HornDuty() float64 {
	return float64(BlastLength) / float64(BlastInterval)
}
