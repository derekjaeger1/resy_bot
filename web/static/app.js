function setupVenueSearch(){
  const input=document.querySelector('[data-venue-search]');
  if(!input)return;
  const results=document.getElementById('venue-results');
  const picked=document.getElementById('venue-picked');
  const venueProvider=document.getElementById('venue-provider');
  const providerVenueId=document.getElementById('provider-venue-id');
  const venueName=document.getElementById('restaurant-name');
  const venueSlug=document.getElementById('venue-slug');
  let timer;

  function chooseVenue(id,name,slug='',provider='resy'){
    providerVenueId.value=id;
    venueProvider.value=provider||'resy';
    venueName.value=name;
    if(venueSlug)venueSlug.value=slug||'';
    input.value=name;
    picked.textContent='✓ '+name;
    picked.style.display='block';
    results.style.display='none';
  }

  input.addEventListener('input',()=>{
    clearTimeout(timer);
    const q=input.value.trim();
    providerVenueId.value='';venueProvider.value='resy';venueName.value='';if(venueSlug)venueSlug.value='';
    picked.style.display='none';
    if(q.length<2){results.style.display='none';return}

    timer=setTimeout(async()=>{
      try{
        const r=await fetch('/api/venues/search?q='+encodeURIComponent(q));
        const items=await r.json();
        results.innerHTML='';
        items.forEach(v=>{
          const el=document.createElement('div');
          el.className='search-item';
          el.innerHTML='<b></b><small></small>';
          el.querySelector('b').textContent=v.name;
          el.querySelector('small').textContent=[v.neighborhood,v.cuisine].filter(Boolean).join(' · ');
          el.onclick=()=>chooseVenue(v.id,v.name,v.url_slug||'',v.provider||'resy');
          results.appendChild(el);
        });
        results.style.display=items.length?'block':'none';
      }catch(e){results.style.display='none'}
    },450);
  });

  document.querySelectorAll('[data-saved-venue]').forEach(b=>{
    b.addEventListener('click',()=>chooseVenue(
      b.dataset.venueId,b.dataset.venueName,b.dataset.venueSlug||'',b.dataset.venueProvider||'resy'
    ));
  });
}

function setupRanges(){
  const dateStart=document.getElementById('date-start');
  const dateEnd=document.getElementById('date-end');
  if(dateStart&&dateEnd){
    dateStart.addEventListener('change',()=>{
      dateEnd.min=dateStart.value;
      if(!dateEnd.value||dateEnd.value<dateStart.value)dateEnd.value=dateStart.value;
    });
  }

  const start=document.getElementById('time-start');
  const end=document.getElementById('time-end');
  document.querySelectorAll('[data-range-start]').forEach(b=>{
    b.addEventListener('click',()=>{
      start.value=b.dataset.rangeStart;
      end.value=b.dataset.rangeEnd;
      document.querySelectorAll('[data-range-start]').forEach(x=>x.classList.remove('selected'));
      b.classList.add('selected');
    });
  });
}

document.addEventListener('DOMContentLoaded',()=>{
  setupVenueSearch();
  setupRanges();
});

function setupLiveAvailability(){
  const feeds=[...document.querySelectorAll('[data-live-group]')];
  if(!feeds.length)return;

  async function refresh(feed){
    const group=feed.dataset.liveGroup;
    const slotsBox=feed.querySelector('.live-feed-slots');
    const timeBox=feed.querySelector('.live-feed-time');
    try{
      const r=await fetch('/api/watch-groups/'+encodeURIComponent(group)+'/availability',{cache:'no-store'});
      const data=await r.json();

      if(data.status==='inactive'){
        timeBox.textContent='Completed';
        slotsBox.innerHTML='<span class="live-empty">Watch completed or stopped.</span>';
        return;
      }

      timeBox.textContent=data.checked_at ? 'Checked '+data.checked_at : 'Waiting for first check';
      slotsBox.innerHTML='';

      if(!data.slots||!data.slots.length){
        slotsBox.innerHTML='<span class="live-empty">No qualifying tables at the last watcher check</span>';
        return;
      }

      data.slots.forEach(slot=>{
        const pill=document.createElement('span');
        pill.className='live-slot';
        const date=new Date(slot.date+'T12:00:00');
        const dateLabel=date.toLocaleDateString([], {month:'short',day:'numeric'});
        pill.textContent=dateLabel+' · '+slot.label;
        slotsBox.appendChild(pill);
      });
    }catch(e){
      timeBox.textContent='Refresh failed';
      slotsBox.innerHTML='<span class="live-empty">Could not load cached watcher status</span>';
    }
  }

  feeds.forEach(feed=>{
    refresh(feed);
    setInterval(()=>refresh(feed),30000);
  });
}

document.addEventListener('DOMContentLoaded',setupLiveAvailability);

function setupBookingCancellation(){
  document.querySelectorAll('[data-cancel-booking]').forEach(form=>{
    form.addEventListener('submit',event=>{
      const restaurant=form.dataset.restaurant||'this reservation';
      const date=form.dataset.date||'';
      const time=form.dataset.time||'';
      const ok=window.confirm(
        'Cancel '+restaurant+'\n'+[date,time].filter(Boolean).join(' · ')+'?\n\nThis will cancel the reservation in Resy. Any restaurant cancellation policy or fee still applies.'
      );
      if(!ok)event.preventDefault();
    });
  });
}
document.addEventListener('DOMContentLoaded',setupBookingCancellation);
