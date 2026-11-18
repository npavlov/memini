# frozen_string_literal: true

# Tea shop counter: a synthetic fixture for the chunker tests.
require "json"

OPENING_HOUR = 8

module Counter
  # One pot of tea on the counter.
  class Pot
    include Comparable
    attr_reader :leaves, :cups

    STEEP_MINUTES = 4

    def initialize(leaves, cups = 2)
      @leaves = leaves
      @cups = cups
      @poured = 0
    end

    def self.small(leaves)
      new(leaves, 1)
    end

    class << self
      def large(leaves)
        new(leaves, 6)
      end

      def sizes
        [1, 2, 6]
      end
    end

    def empty? = @poured >= @cups

    def pour!
      raise "the pot is empty" if empty?

      @poured += 1
      cups - @poured
    end

    def <=>(other)
      cups <=> other.cups
    end

    def to_json(*args)
      { leaves: leaves, cups: cups }.to_json(*args)
    end

    private

    def steeped?(minutes)
      minutes >= STEEP_MINUTES
    end
  end
end

class Counter::Pot
  def refill
    @poured = 0
  end
end

def open?(hour)
  hour >= OPENING_HOUR
end

puts Counter::Pot.small("mint").pour! if $PROGRAM_NAME == __FILE__
