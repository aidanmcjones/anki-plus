// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Historical knowledge series ("learning velocity") computed by replaying
//! the review log through FSRS. For each past day we estimate the total
//! knowledge stock: the sum over cards of the probability of recall on that
//! day, given the reviews up to that point. The client derives velocity
//! (change per day) and efficiency (change per hour studied) from this.

use std::collections::HashMap;

use anki_proto::stats::graphs_response::Velocity;
use fsrs::FSRS;

use crate::card::CardId;
use crate::deckconfig::DeckConfigId;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::memory_state::get_decay_from_params;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::stats::graphs::GraphsContext;

/// Memory state at a given day, valid until the next segment (or today).
struct Segment {
    day: i32,
    state: fsrs::MemoryState,
}

impl GraphsContext {
    pub(super) fn velocity(&self) -> Option<Velocity> {
        if !self.fsrs_enabled {
            return None;
        }
        // revlog is time-ordered; group per card, preserving order
        let mut revlog_by_card: HashMap<CardId, Vec<&RevlogEntry>> = HashMap::new();
        for e in &self.revlog {
            revlog_by_card.entry(e.cid).or_default().push(e);
        }
        // card -> params key (use the original deck for cards in filtered decks)
        let card_config: HashMap<CardId, DeckConfigId> = self
            .cards
            .iter()
            .filter_map(|c| {
                let deck = if c.original_deck_id.0 != 0 {
                    c.original_deck_id
                } else {
                    c.deck_id
                };
                self.deck_to_config.get(&deck).map(|cid| (c.id, *cid))
            })
            .collect();
        let mut fsrs_cache: HashMap<DeckConfigId, Option<(FSRS, f32)>> = HashMap::new();

        let mut segments_per_card: Vec<Vec<Segment>> = vec![];
        let mut decay_per_card: Vec<f32> = vec![];
        let mut min_day = 0i32;
        for (cid, entries) in revlog_by_card {
            let Some(config_id) = card_config.get(&cid) else {
                continue;
            };
            let Some((fsrs, decay)) = fsrs_cache
                .entry(*config_id)
                .or_insert_with(|| {
                    let params = self.config_params.get(config_id)?;
                    let fsrs = FSRS::new(&params[..]).ok()?;
                    Some((fsrs, get_decay_from_params(params)))
                })
                .as_ref()
            else {
                continue;
            };
            let entries: Vec<RevlogEntry> = entries.into_iter().cloned().collect();
            let Some(output) =
                reviews_for_fsrs(entries, self.next_day_start, false, TimestampMillis(0))
            else {
                continue;
            };
            // For truncated logs, derive a starting state from the first
            // entry's SM-2 data, mirroring fsrs_item_for_memory_state().
            let starting_state = if output.revlogs_complete {
                None
            } else if let Some(first) = output.filtered_revlogs.first() {
                let ease_factor = if first.ease_factor == 0 {
                    2500
                } else {
                    first.ease_factor
                } as f32
                    / 1000.0;
                let interval = first.interval.max(1) as f32;
                let Ok(mut state) = fsrs.memory_state_from_sm2(ease_factor, interval, 0.9) else {
                    continue;
                };
                if ease_factor <= 1.1 {
                    state.difficulty = (ease_factor - 0.1) * 9.0 + 1.0;
                }
                Some(state)
            } else {
                continue;
            };
            // Build cumulative review prefixes ourselves: fsrs_items only
            // contains the final full-history item when not training. The
            // delta_t construction mirrors reviews_for_fsrs().
            let entries = &output.filtered_revlogs;
            let mut reviews: Vec<fsrs::FSRSReview> = Vec::with_capacity(entries.len());
            let mut prev_days_elapsed = None;
            let mut segments: Vec<Segment> = vec![];
            for entry in entries {
                let days_elapsed = (self.next_day_start.elapsed_secs_since(entry.id.as_secs())
                    / 86_400)
                    .max(0) as u32;
                let delta_t = prev_days_elapsed
                    .map(|prev: u32| prev.saturating_sub(days_elapsed))
                    .unwrap_or(0);
                prev_days_elapsed = Some(days_elapsed);
                reviews.push(fsrs::FSRSReview {
                    rating: entry.button_chosen as u32,
                    delta_t,
                });
                let effective: Vec<fsrs::FSRSReview> = if starting_state.is_some() {
                    // first review was converted into the starting state
                    reviews[1..].to_vec()
                } else {
                    reviews.clone()
                };
                let state = if effective.is_empty() {
                    match starting_state {
                        Some(s) => s,
                        None => continue,
                    }
                } else {
                    match fsrs.memory_state(fsrs::FSRSItem { reviews: effective }, starting_state) {
                        Ok(s) => s,
                        Err(err) => {
                            tracing::debug!("velocity: skipping card {cid}: {err:?}");
                            segments.clear();
                            break;
                        }
                    }
                };
                let day =
                    (entry.id.as_secs().elapsed_secs_since(self.next_day_start) / 86_400) as i32;
                let day = day.min(0);
                if segments.last().map(|s| s.day) == Some(day) {
                    // same-day review: keep the later state
                    segments.pop();
                }
                segments.push(Segment { day, state });
            }
            if let Some(first) = segments.first() {
                min_day = min_day.min(first.day);
                segments_per_card.push(segments);
                decay_per_card.push(*decay);
            }
        }

        let span_days = (-min_day) as u32 + 1;
        let res = (span_days / 1095).max(1) as i32;
        let mut knowledge: HashMap<i32, f32> = HashMap::new();
        for (segments, decay) in segments_per_card.iter().zip(decay_per_card.iter()) {
            for (i, seg) in segments.iter().enumerate() {
                let end = segments.get(i + 1).map(|s| s.day).unwrap_or(1);
                // first sampled day >= segment start, aligned so day 0 is
                // always on the grid
                let mut d = seg.day - seg.day.rem_euclid(res);
                if d < seg.day {
                    d += res;
                }
                while d < end {
                    *knowledge.entry(d).or_insert(0.0) +=
                        fsrs::current_retrievability(seg.state, (d - seg.day) as f32, *decay);
                    d += res;
                }
            }
        }
        Some(Velocity {
            knowledge,
            resolution_days: res as u32,
        })
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::deckconfig::DeckConfig;
    use crate::revlog::RevlogReviewKind;

    fn context(revlog: Vec<RevlogEntry>, cards: Vec<Card>, fsrs_enabled: bool) -> GraphsContext {
        let config = DeckConfig::default();
        GraphsContext {
            revlog,
            cards,
            next_day_start: TimestampSecs(1_700_000_000),
            days_elapsed: 100,
            local_offset_secs: 0,
            fsrs_enabled,
            deck_to_config: [(DeckId(1), DeckConfigId(1))].into_iter().collect(),
            config_params: [(DeckConfigId(1), config.fsrs_params().clone())]
                .into_iter()
                .collect(),
        }
    }

    fn card() -> Card {
        Card {
            id: CardId(1),
            deck_id: DeckId(1),
            ..Default::default()
        }
    }

    fn entry(days_ago: i64, button: u8, kind: RevlogReviewKind, interval: i32) -> RevlogEntry {
        RevlogEntry {
            id: RevlogId((1_700_000_000 - days_ago * 86_400 - 3_600) * 1000),
            cid: CardId(1),
            button_chosen: button,
            interval,
            last_interval: 0,
            ease_factor: 2500,
            review_kind: kind,
            ..Default::default()
        }
    }

    #[test]
    fn disabled_fsrs_returns_none() {
        let ctx = context(vec![], vec![card()], false);
        assert!(ctx.velocity().is_none());
    }

    #[test]
    fn empty_revlog_returns_empty_map() {
        let ctx = context(vec![], vec![card()], true);
        let v = ctx.velocity().unwrap();
        assert!(v.knowledge.is_empty());
        assert_eq!(v.resolution_days, 1);
    }

    #[test]
    fn two_reviews_produce_decaying_series() {
        let revlog = vec![
            entry(10, 3, RevlogReviewKind::Learning, 1),
            entry(3, 3, RevlogReviewKind::Review, 7),
        ];
        let ctx = context(revlog, vec![card()], true);
        let v = ctx.velocity().unwrap();
        assert_eq!(v.resolution_days, 1);
        // covers first review day through today
        for d in -10..=0 {
            let k = *v
                .knowledge
                .get(&d)
                .unwrap_or_else(|| panic!("missing day {d}; got {:?}", v.knowledge));
            assert!(k > 0.0 && k <= 1.0, "day {d} = {k}");
        }
        // retrievability decays between reviews
        assert!(v.knowledge[&-4] < v.knowledge[&-9]);
        // and resets upward after the second review
        assert!(v.knowledge[&-3] > v.knowledge[&-4]);
        // nothing before the first review
        assert!(!v.knowledge.contains_key(&-11));
    }
}
