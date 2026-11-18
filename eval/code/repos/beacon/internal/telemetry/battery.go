package telemetry

// Пороги заряда батареи в процентах.
const (
	WarningLevel = 40
	AlarmLevel   = 20
)

// Severity — уровень важности тревоги.
type Severity int

const (
	SeverityNone Severity = iota
	SeverityWarning
	SeverityAlarm
)

// BatterySeverity возвращает уровень тревоги для заряда батареи: предупреждение ниже сорока
// процентов, тревога ниже двадцати.
func BatterySeverity(percent int) Severity {
	switch {
	case percent < AlarmLevel:
		return SeverityAlarm
	case percent < WarningLevel:
		return SeverityWarning
	default:
		return SeverityNone
	}
}

// NightsLeft оценивает, сколько ночей маяк продержится на батареях без подзарядки.
func NightsLeft(percent int, percentPerNight float64) float64 {
	if percentPerNight <= 0 {
		return 0
	}
	return float64(percent) / percentPerNight
}
