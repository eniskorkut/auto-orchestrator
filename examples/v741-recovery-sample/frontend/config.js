/* Runtime configuration for the task app.
 *
 * Empty string means "same origin as the page", which is how the backend
 * serves the frontend in development. A deployment that hosts the API
 * elsewhere can set window.AO_API_BASE before this script runs.
 */
if (typeof window.AO_API_BASE !== "string") {
  window.AO_API_BASE = "";
}
