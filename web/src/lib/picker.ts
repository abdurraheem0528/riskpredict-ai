// Google Picker for the drive.file scope (contract decision 7). The Picker is Google's
// own UI, loaded from apis.google.com only when the user asks for it. The files the user
// picks become readable by the API; nothing else in their Drive does.
type PickerDoc = { id: string; name: string; url?: string };
type GoogleNS = {
  picker: {
    PickerBuilder: new () => PickerBuilder;
    DocsView: new (viewId: unknown) => { setMimeTypes(m: string): unknown; setMode(m: unknown): unknown };
    ViewId: { SPREADSHEETS: unknown };
    DocsViewMode: { LIST: unknown };
    Action: { PICKED: string; CANCEL: string };
    Feature: { NAV_HIDDEN: unknown };
  };
};
type PickerBuilder = {
  addView(v: unknown): PickerBuilder;
  setOAuthToken(t: string): PickerBuilder;
  setDeveloperKey(k: string): PickerBuilder;
  setAppId(id: string): PickerBuilder;
  setTitle(t: string): PickerBuilder;
  enableFeature(f: unknown): PickerBuilder;
  setCallback(cb: (d: { action: string; docs?: PickerDoc[] }) => void): PickerBuilder;
  build(): { setVisible(v: boolean): void };
};
declare global {
  interface Window {
    gapi?: { load(name: string, cb: () => void): void };
    google?: GoogleNS;
  }
}

let loading: Promise<GoogleNS> | null = null;
function load(): Promise<GoogleNS> {
  if (!loading) {
    loading = new Promise<GoogleNS>((resolve, reject) => {
      const done = () => window.gapi!.load('picker', () => (window.google ? resolve(window.google) : reject(new Error('Picker did not load'))));
      if (window.gapi) return done();
      const s = document.createElement('script');
      s.src = 'https://apis.google.com/js/api.js';
      s.async = true;
      s.onload = done;
      s.onerror = () => {
        loading = null;
        reject(new Error('Could not load the Google Picker'));
      };
      document.head.appendChild(s);
    });
  }
  return loading;
}

/** Opens the Picker; resolves with the chosen spreadsheet, or null if cancelled. */
export async function pickSpreadsheet(creds: { access_token: string; developer_key: string; app_id: string }): Promise<PickerDoc | null> {
  const g = await load();
  return new Promise((resolve) => {
    const view = new g.picker.DocsView(g.picker.ViewId.SPREADSHEETS);
    view.setMode(g.picker.DocsViewMode.LIST);
    new g.picker.PickerBuilder()
      .addView(view)
      .enableFeature(g.picker.Feature.NAV_HIDDEN)
      .setTitle('Pick the sheet with your suppliers, sites or shipments')
      .setOAuthToken(creds.access_token)
      .setDeveloperKey(creds.developer_key)
      .setAppId(creds.app_id)
      .setCallback((d) => {
        if (d.action === g.picker.Action.PICKED && d.docs?.[0]) resolve(d.docs[0]);
        else if (d.action === g.picker.Action.CANCEL) resolve(null);
      })
      .build()
      .setVisible(true);
  });
}
