package telemetry

import (
	"encoding/json"
	"net"
	"time"

	"beacon/internal/lamp"
)

// Packet — пакет телеметрии, который агент отправляет на берег раз в минуту.
type Packet struct {
	At             time.Time `json:"at"`
	BatteryPercent int       `json:"battery_percent"`
	TurnsPerMinute float64   `json:"turns_per_minute"`
	Visibility     int       `json:"visibility_m"`
	HornOn         bool      `json:"horn_on"`
}

var (
	queue  []Packet
	hornOn bool
)

// Collect снимает показания датчиков в один пакет.
func Collect(l *lamp.Lamp) Packet {
	visibility := readVisibility()
	hornOn = ShouldSoundHorn(visibility, hornOn)
	return Packet{
		At:             time.Now().UTC(),
		BatteryPercent: readBattery(),
		TurnsPerMinute: float64(time.Minute) / float64(l.Period),
		Visibility:     visibility,
		HornOn:         hornOn,
	}
}

// Send отправляет пакет на береговую станцию одной строкой JSON.
func Send(station string, p Packet) error {
	conn, err := net.DialTimeout("tcp", station, 5*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	return json.NewEncoder(conn).Encode(p)
}

// Enqueue кладёт неотправленный пакет в очередь, пока нет связи с берегом.
func Enqueue(p Packet) {
	queue = append(queue, p)
}

// Flush отправляет накопленную очередь по порядку и останавливается на первой ошибке.
func Flush(station string) {
	for len(queue) > 0 {
		if Send(station, queue[0]) != nil {
			return
		}
		queue = queue[1:]
	}
}

func readBattery() int    { return 100 }
func readVisibility() int { return 10000 }
