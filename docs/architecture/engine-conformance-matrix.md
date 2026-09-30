# Engine OLTP conformance matrix

The matrix is generated from registry claims plus explicit qualification evidence.
Durability may describe a declared baseline capability, but AUTHORITATIVE is never
inferred from capability flags. Only a `PersistenceQualification` produced by the
executable authoritative suite can promote an adapter/configuration envelope.

This makes unsupported gaps visible without forcing every adapter to implement
ownership, fencing, or distributed execution.
