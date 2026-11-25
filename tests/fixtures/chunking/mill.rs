// mill.rs is a synthetic fixture for the chunker tests.
use std::collections::HashMap;

pub const STONES: usize = 2;

/// Grain the mill knows how to grind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Grain {
    Wheat,
    Rye,
}

pub trait Grind {
    fn grind(&mut self, grain: Grain, kilograms: u32) -> u32;
}

pub struct Mill {
    flour: HashMap<Grain, u32>,
}

impl Mill {
    pub fn new() -> Self {
        Mill { flour: HashMap::new() }
    }

    /// Kilograms of flour of one grain.
    pub fn stock(&self, grain: Grain) -> u32 {
        *self.flour.get(&grain).unwrap_or(&0)
    }
}

impl Grind for Mill {
    fn grind(&mut self, grain: Grain, kilograms: u32) -> u32 {
        let flour = kilograms * 9 / 10;
        *self.flour.entry(grain).or_insert(0) += flour;
        flour
    }
}

type Ledger = HashMap<String, u32>;

mod sacks {
    pub fn weight(count: u32) -> u32 {
        count * 25
    }
}

fn main() {
    let mut mill = Mill::new();
    mill.grind(Grain::Rye, 100);
    println!("{}", mill.stock(Grain::Rye) + sacks::weight(1));
}
