/**
 * Backend prefixes this application is allowed to reach.
 *
 * `v1/brief` is listed in its own right. The brief used to live under
 * `v1/turns/{i}/brief` and so was reachable through the `v1/turns` prefix by
 * accident; moving it to the session scope took it outside every prefix here,
 * and the proxy answered a 404 of its own making.
 */
export const ALLOWED_PREFIXES = [
  "v1/turns",
  "v1/brief",
  "v1/session",
  "v1/transparency",
  "readyz",
] as const;
