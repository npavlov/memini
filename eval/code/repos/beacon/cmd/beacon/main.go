package main

import (
	"flag"
	"log"
	"time"

	"beacon/internal/lamp"
	"beacon/internal/telemetry"
)

// main читает флаги, затем по таймеру опрашивает датчики и отправляет пакет на берег.
// Пакет, который не удалось отправить, ставится в очередь и уйдёт позже.
func main() {
	interval := flag.Duration("interval", 30*time.Second, "период опроса датчиков")
	station := flag.String("station", "shore.example.invalid:7000", "адрес береговой станции")
	flag.Parse()

	l := lamp.New(lamp.RotationPeriod)
	for range time.Tick(*interval) {
		packet := telemetry.Collect(l)
		if err := telemetry.Send(*station, packet); err != nil {
			log.Printf("пакет не отправлен, ставлю в очередь: %v", err)
			telemetry.Enqueue(packet)
			continue
		}
		telemetry.Flush(*station)
	}
}
