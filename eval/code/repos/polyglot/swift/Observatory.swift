// Observatory.swift: a telescope and the stars it sees.
import Foundation

let parsecKilometres = 30_856_775_814_913.7

/// Anything that gathers starlight.
protocol Mirror {
    func magnify(_ value: Double) -> Double
}

enum Phase {
    case new
    case full
}

struct Star {
    let name: String
    var magnitude: Double
}

final class Telescope: Mirror {
    private var stars: [Star] = []

    init(stars: [Star]) {
        self.stars = stars
    }

    /// The brightest star in view.
    func brightest() -> Star? {
        return stars.min { $0.magnitude < $1.magnitude }
    }

    func magnify(_ value: Double) -> Double {
        return value * 40
    }
}

extension Telescope {
    func catalogue() -> [String] {
        return stars.map { $0.name }
    }
}

func observe(_ telescope: Telescope) -> String {
    return telescope.brightest()?.name ?? "none"
}
