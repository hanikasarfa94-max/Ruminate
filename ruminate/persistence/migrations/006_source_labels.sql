-- Keep a user-chosen label across imports, even if a later original title matches it.
ALTER TABLE sources ADD COLUMN title_is_custom INTEGER NOT NULL DEFAULT 0
    CHECK (title_is_custom IN (0, 1));
