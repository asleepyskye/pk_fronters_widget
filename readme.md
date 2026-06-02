yeah, i'll write a more proper readme for this when i dont have a headache

```
(async ()=>{let _mods=webpackChunkdiscord_app.push([[Symbol()],{},e=>e.c]);webpackChunkdiscord_app.pop(); let findByProps=(...e)=>{for(let t of Object.values(_mods))try{if(!t.exports||t.exports===window)continue;if(e.every(e=>t.exports?.[e]))return t.exports;for(let r in t.exports)if(e.every(e=>t.exports?.[r]?.[e])&&"IntlMessagesProxy"!==t.exports[r][Symbol.toStringTag])return t.exports[r]}catch{}}; let api = Object.values(_mods).find(x => x?.exports?.Bo?.get).exports.Bo; let id = findByProps("getCurrentUser").getCurrentUser().id; let current_widgets = (await api.get("/users/" + id + "/profile")).body.widgets; if (current_widgets.map(x=>x.data?.application_id).includes("1511072634232770621")) {return console.log("Already in your widgets — remove it via Discord client to re-add");} current_widgets.unshift({"data":{"type":"application","application_id":"1511072634232770621"}}); await api.put({url:"/users/@me/widgets",body:{widgets:current_widgets}});})()
```

special thanks to https://github.com/chloecinders/xivwidget