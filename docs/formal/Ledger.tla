---------------------------- MODULE Ledger ----------------------------
(*
  Formal Specification of Ledger Bitemporal Interval Derivation State Machine
  Author: Ledger Quantitative Data Engineering Core
  Purpose: Model-check bitemporal derivation invariants across bounded state spaces.
*)

EXTENDS Integers, Sequences, FiniteSets, TLC

CONSTANTS
    SecIDs,          \* Finite set of security IDs (e.g., {"SEC_AAPL", "SEC_TSLA"})
    MaxTime,         \* Integer upper bound for timestamp domain (e.g., 0..5)
    MaxEvents        \* Bound on maximum sequence length of ingested events (e.g., 4)

VARIABLES
    ingested,        \* Sequence of ingested records: << [sec_id, known_from, valid_from, ingestion_seq], ... >>
    nextSeq          \* Monotonic sequence counter for tie-breaking

vars == << ingested, nextSeq >>

\* Helper: Convert sequence to set
Range(s) == { s[i] : i \in DOMAIN s }

\* Helper: Check if two half-open intervals [s1, e1) and [s2, e2) overlap.
\* Note: -1 represents NULL / +infinity.
Overlaps(s1, e1, s2, e2) ==
    LET e1_effective == IF e1 = -1 THEN MaxTime + 1 ELSE e1
        e2_effective == IF e2 = -1 THEN MaxTime + 1 ELSE e2
    IN s1 < e2_effective /\ s2 < e1_effective

\* Derivation operator: Compute derived known_to for a row using LEAD() semantics.
KnownTo(row) ==
    LET laterRows == { r \in Range(ingested) :
                          r.sec_id = row.sec_id
                          /\ (r.known_from > row.known_from
                              \/ (r.known_from = row.known_from /\ r.ingestion_seq > row.ingestion_seq)) }
    IN IF laterRows = {} THEN -1
       ELSE LET minRow == CHOOSE r \in laterRows :
                              \A r2 \in laterRows :
                                  r.known_from < r2.known_from
                                  \/ (r.known_from = r2.known_from /\ r.ingestion_seq <= r2.ingestion_seq)
            IN minRow.known_from

\* Derivation operator: Compute derived valid_to for a row using LEAD() semantics.
ValidTo(row) ==
    LET laterRows == { r \in Range(ingested) :
                          r.sec_id = row.sec_id
                          /\ (r.valid_from > row.valid_from
                              \/ (r.valid_from = row.valid_from /\ r.ingestion_seq > row.ingestion_seq)) }
    IN IF laterRows = {} THEN -1
       ELSE LET minRow == CHOOSE r \in laterRows :
                              \A r2 \in laterRows :
                                  r.valid_from < r2.valid_from
                                  \/ (r.valid_from = r2.valid_from /\ r.ingestion_seq <= r2.ingestion_seq)
            IN minRow.valid_from

\* Transition Action: Ingest a new observation fact.
Ingest(sid, kf, vf) ==
    /\ Len(ingested) < MaxEvents
    /\ kf \in 0..MaxTime
    /\ vf \in 0..MaxTime
    /\ kf >= vf   \* Enforce temporal causality: known_from >= valid_from
    /\ ingested' = Append(ingested, [sec_id |-> sid, known_from |-> kf,
                                       valid_from |-> vf, ingestion_seq |-> nextSeq])
    /\ nextSeq' = nextSeq + 1

Init ==
    /\ ingested = << >>
    /\ nextSeq = 0

Next ==
    \E sid \in SecIDs, kf, vf \in 0..MaxTime : Ingest(sid, kf, vf)

Spec == Init /\ [][Next]_vars

\* ============================ INVARIANTS ============================

\* Invariant 1: NoOverlap - No two records for the same entity have overlapping [known_from, known_to) intervals
NoOverlap ==
    \A i, j \in DOMAIN ingested :
        (i /= j /\ ingested[i].sec_id = ingested[j].sec_id)
        => ~Overlaps(ingested[i].known_from, KnownTo(ingested[i]),
                     ingested[j].known_from, KnownTo(ingested[j]))

\* Invariant 2: Monotonic - Transaction start time strictly precedes derived known_to (or known_to is infinity)
Monotonic ==
    \A row \in Range(ingested) :
        KnownTo(row) = -1 \/ row.known_from < KnownTo(row)

\* Invariant 3: NoGaps - Adjacent versions of knowledge for an entity leave no un-covered temporal gaps
NoGaps ==
    \A row \in Range(ingested) :
        KnownTo(row) /= -1 =>
            \E nextRow \in Range(ingested) :
                nextRow.sec_id = row.sec_id /\ nextRow.known_from = KnownTo(row)

\* Invariant 4: ValidBeforeKnown - A fact cannot be known prior to its real-world valid start date
ValidBeforeKnown ==
    \A row \in Range(ingested) :
        row.known_from >= row.valid_from

\* Invariant 5: IdempotentReplay - Identical re-ingestions break ties deterministically without interval corruption
IdempotentReplay ==
    \A i, j \in DOMAIN ingested :
        (i /= j /\ ingested[i].sec_id = ingested[j].sec_id
                /\ ingested[i].known_from = ingested[j].known_from
                /\ ingested[i].valid_from = ingested[j].valid_from)
        => (KnownTo(ingested[i]) = KnownTo(ingested[j])
            \/ KnownTo(ingested[i]) = ingested[j].known_from
            \/ KnownTo(ingested[j]) = ingested[i].known_from)

=============================================================================
