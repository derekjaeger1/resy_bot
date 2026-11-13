function setupVenueSearch(){
  const input=document.querySelector('[data-venue-search]');
  if(!input)return;
  const results=document.getElementById('venue-results');
  const picked=document.getElementById('venue-picked');
  const venueId=document.getElementById('venue-id');
  const venueName=document.getElementById('restaurant-name');
  const venueSlug=document.getElementById('venue-slug');
  let timer;

  function chooseVenue(id,name,slug=''){
    venueId.value=id;
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
    venueId.value='';venueName.value='';if(venueSlug)venueSlug.value='';
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
          el.onclick=()=>chooseVenue(v.id,v.name,v.url_slug||'');
          results.appendChild(el);
        });
        results.style.display=items.length?'block':'none';
      }catch(e){results.style.display='none'}
    },220);
  });

  document.querySelectorAll('[data-saved-venue]').forEach(b=>{
    b.addEventListener('click',()=>chooseVenue(
      b.dataset.venueId,b.dataset.venueName,b.dataset.venueSlug||''
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
