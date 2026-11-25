import { APPLICATIONS } from "../chat/applications";

/** The programs that have a memory of their own (ARCHITECTURE D74): every application the hub knows
 *  (proto/ui/applications.json), Projects included. */
export const PROGRAMS = APPLICATIONS;

/** Facts about a place and the notes of a day of work always belong to a program, never the core. */
export const PROGRAM_KINDS: readonly string[] = ["operational"];
