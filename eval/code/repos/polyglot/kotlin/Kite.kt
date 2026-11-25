// Kite.kt: kites and the breeze they fly in.
package town.kites

import kotlin.math.max

const val MAX_STRING_FEET = 200

/** Anything that flies on a string. */
interface Flyer {
    fun lift(breeze: Int): Int
}

enum class Gust { CALM, FRESH, STRONG }

data class Kite(val colour: String, val tails: Int) : Flyer {
    override fun lift(breeze: Int): Int {
        return max(0, breeze - tails)
    }

    fun shorten(): Kite = copy(tails = tails - 1)

    companion object {
        fun plain(): Kite = Kite("white", 0)
    }
}

object Breeze {
    var pace = 3

    fun gust(): Gust = if (pace > 10) Gust.STRONG else Gust.FRESH
}

fun Kite.launch(breeze: Int): Boolean = lift(breeze) > 2

fun sky(kites: List<Kite>): Int {
    var sum = 0
    for (kite in kites) {
        sum += kite.tails
    }
    return sum
}
