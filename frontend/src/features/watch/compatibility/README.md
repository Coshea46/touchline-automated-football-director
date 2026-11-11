# Compatibility player coverage

The running Watch view uses `playback/PersistentMatchPlayer.js`, with seven
mounted players and promotion of the focused element.

`MainPlayer.js` and `VideoTile.js` are retained only for existing compatibility
tests. Those tests exercise source reloads, native audio behavior, source identity,
caption synchronization and duration bounds. Neither component is imported by the
application. Keep these tests until each relevant contract has equivalent
coverage on the persistent-player implementation; removing the components and
their tests together would discard that evidence.

The unused earlier `FeedGrid` and `FocusView` screens had no source or test callers
and have been removed. New application code should use `WatchView` and the
persistent player rather than reviving the earlier player tree.
