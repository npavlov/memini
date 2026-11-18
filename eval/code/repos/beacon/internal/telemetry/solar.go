package telemetry

// ChargeEfficiency — доля энергии солнечной панели, которая доходит до батарей.
const ChargeEfficiency = 0.8

// SolarDay — выработка солнечной панели за день в ватт-часах.
type SolarDay struct {
	Date      string
	WattHours float64
}

// ChargeGain считает, на сколько процентов поднимется заряд батарей за день при данной
// выработке панели и ёмкости батарей в ватт-часах. Заряд не превышает ста процентов.
func ChargeGain(day SolarDay, capacityWh float64, percentNow int) int {
	if capacityWh <= 0 {
		return 0
	}
	gain := int(day.WattHours * ChargeEfficiency / capacityWh * 100)
	if percentNow+gain > 100 {
		return 100 - percentNow
	}
	return gain
}

// GreyDays считает дни подряд с конца списка, когда панель дала меньше нормы.
func GreyDays(days []SolarDay, normWh float64) int {
	count := 0
	for i := len(days) - 1; i >= 0; i-- {
		if days[i].WattHours >= normWh {
			break
		}
		count++
	}
	return count
}
