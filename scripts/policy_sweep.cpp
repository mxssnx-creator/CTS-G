// Event-based causal sweep. No exchange access. Build via sweep_seven_days.py.
#include <algorithm>
#include <cmath>
#include <vector>

extern "C" void sweep(int n, const int* entry, const int* close, const double* net,
                      const double* cost, int np, const int* policies, int endbar,
                      int split, double* out) {
  // Columns: N, win, net, costs, gain, loss, maxDD, maxDDTbars,
  // trainN, trainNet, testN, testNet, disabled, admitted cost ratios sum.
  std::vector<double> sums(n+1), ratios(n+1);
  std::vector<int> available(n), age(n);
  double eq=0, peak=0; int ddstart=-1, a=0;
  for(int j=0;j<n;j++){
    sums[j+1]=sums[j]+net[j];
    ratios[j+1]=ratios[j]+net[j]/std::max(cost[j],1e-12);
    while(a<j && close[a]<entry[j]){
      eq+=net[a];
      if(eq>=peak-1e-12){peak=std::max(peak,eq);ddstart=-1;}
      else if(ddstart<0)ddstart=close[a];
      a++;
    }
    available[j]=a;
    age[j]=ddstart<0?0:entry[j]-ddstart;
  }
  for(int p=0;p<np;p++){
    const int* pol=policies+p*6;
    const int window=pol[0], deact=pol[1], maxage=pol[2]*60, cadence=pol[3];
    const int mn=pol[4],rn=pol[5];
    double* r=out+p*14;
    double live=0, high=0, tail=0;int underwater=-1, count=0, checked=-1;
    bool valid=false,disabled=false;
    std::vector<double> recent(deact,0);
    for(int j=0;j<n;j++){
      int seen=available[j];
      if(seen<window || seen<mn || seen<rn)continue;
      // First complete sample publishes immediately; then only new closes
      // crossing the selected cadence publish a new evaluation.
      if(checked<0 || seen-checked>=cadence){
        checked=seen;
        auto ok=[&](int k){return (1.+.1*(ratios[seen]-ratios[seen-k])/k)>1.05+1e-9;};
        valid=ok(window)&&ok(mn)&&ok(rn);
      }
      if(!valid || age[j]>maxage || disabled)continue;
      // Each lane has non-overlapping positions. Every earlier admitted
      // close is known before this entry; its outcome cannot gate itself.
      double v=net[j];live+=v;
      r[0]++;r[1]+=v>0;r[2]+=v;r[3]+=cost[j];r[4]+=std::max(0.,v);r[5]+=std::max(0.,-v);
      r[13]+=v/std::max(cost[j],1e-12);
      if(close[j]<split){r[8]++;r[9]+=v;}else{r[10]++;r[11]+=v;}
      if(live>=high-1e-12){
        if(underwater>=0)r[7]=std::max(r[7],double(close[j]-underwater));
        high=std::max(high,live);underwater=-1;
      }else if(underwater<0)underwater=close[j];
      r[6]=std::max(r[6],high-live);
      if(underwater>=0)r[7]=std::max(r[7],double(close[j]-underwater));
      tail-=recent[count%deact];recent[count%deact]=v;tail+=v;count++;
      if(count>=deact && tail<-1e-12){disabled=true;r[12]=1;}
    }
    if(underwater>=0)r[7]=std::max(r[7],double(endbar-underwater));
  }
}
