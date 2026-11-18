"use strict";

// Playlist shuffler: a synthetic fixture for the chunker tests.
const path = require("node:path");

const MAX_TRACKS = 500;
let lastSeed = 0;

function seconds(minutes) {
  return minutes * 60;
}

function* cycle(items) {
  while (true) {
    yield* items;
  }
}

/**
 * A playlist of tracks; a track is { title, length }.
 */
class Playlist {
  #tracks = [];
  static count = 0;

  constructor(name) {
    this.name = name;
    Playlist.count += 1;
  }

  add(track) {
    if (this.#tracks.length >= MAX_TRACKS) {
      throw new Error("the playlist is full");
    }
    this.#tracks.push(track);
  }

  get length() {
    return this.#tracks.reduce((sum, track) => sum + track.length, 0);
  }

  shuffle(seed) {
    lastSeed = seed;
    const order = [...this.#tracks];
    for (let i = order.length - 1; i > 0; i -= 1) {
      const j = (seed * (i + 7)) % (i + 1);
      [order[i], order[j]] = [order[j], order[i]];
    }
    return order;
  }

  #describe(track) {
    return `${track.title} (${track.length}s)`;
  }

  onPlay = (track) => {
    console.log(this.#describe(track));
  };
}

const Radio = class extends Playlist {
  live() {
    return true;
  }
};

function TrackList({ tracks }) {
  return (
    <ul>
      {tracks.map((track) => (
        <li key={track.title}>{track.title}</li>
      ))}
    </ul>
  );
}

const helpers = {
  basename(file) {
    return path.basename(file);
  },
};

module.exports = { Playlist, Radio, TrackList, cycle, helpers, seconds };
