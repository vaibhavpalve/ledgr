/**
 * Callback shapes for actions a screen performs on the server.
 *
 * In a `.ts` file on purpose: the translation check scans `.tsx` for text that
 * looks like prose in a component, and `=> Promise<void>` in one reads to it as
 * the word "Promise" written into the screen.
 */

/** Does something and resolves when it is done; rejects with the server's own sentence. */
export type AsyncAction = () => Promise<void>;

/** The same, on one thing - a notice, a row. */
export type AsyncActionOn<Target> = (target: Target) => Promise<void>;
